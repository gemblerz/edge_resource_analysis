import json
import datetime
from pathlib import Path
import os
import hashlib
import logging

import click
import pandas as pd
import matplotlib.pyplot as plt
from sage_data_client import query
import numpy as np
from tqdm import tqdm


pd.set_option('mode.chained_assignment',None)


def download_performance_data(vsn, start, end=""):
    filter={
        "vsn": vsn.upper(),
        "name": "container_cpu_usage_seconds_total|container_memory_rss|container_memory_working_set_bytes|tegra_wattage_current_milliwatts"
    }

    if end == "":
        return query(
            start=start,
            filter=filter,
            bucket="grafana-agent")
    else:
        return query(
            start=start,
            end=end,
            filter=filter,
            bucket="grafana-agent")


def download_scheduler_event(vsn, start, end=""):
    filter={
        "vsn": vsn.upper(),
        "name": "sys.scheduler.status.plugin.launched|sys.scheduler.status.plugin.complete|sys.scheduler.status.plugin.failed"
    }

    if end == "":
        return query(
            start=start,
            filter=filter)
    else:
        return query(
            start=start,
            end=end,
            filter=filter)

DOWNLOAD_TYPE_JOB = "job"
DOWNLOAD_TYPE_PERF = "perf"

def download_bulk_data(download_type, download_func, vsn,
    start, end="",
    window='D', verbose=True):
    path = Path.home().joinpath(f'.waggle/{vsn}/{download_type}')
    os.makedirs(path, exist_ok=True)
    df = pd.DataFrame()

    # if end == "":
    #     end = datetime.datetime.now(datetime.timezone.utc).isoformat()
    ranges = pd.date_range(start=start, end=end, freq="D")
    if end > ranges[-1]:
        ranges = ranges.append(pd.DatetimeIndex([end]))
    start_t = None
    t = tqdm(ranges)
    for date in t:
        if start_t is None:
            start_t = date.isoformat()
            continue
        
        end_t = date.isoformat()
        string = f'{vsn},{start_t},{end_t}'
        md5checksum = hashlib.md5(string.encode())
        filename = f'{md5checksum.hexdigest()}.csv'
        cache_path = path.joinpath(filename)
        t.write(f'{vsn}: Querying {start_t} - {end_t}')
        if cache_path.exists():
            t.write(f'{vsn}: Cache found {cache_path}. Reading the file instead of downloading.')
            _df = pd.read_csv(cache_path)
        else:
            t.write(f'{vsn}: Downloading {start_t} - {end_t}')
            _df = download_func(vsn, start_t, end_t)
            _df.to_csv(cache_path, index=False)
            t.write(f'{vsn}: Saving to {cache_path}')
        df = pd.concat([df, _df])
        start_t = end_t
    return df

def generate_job_records(df):
    # Just to ensure the timestamp is in the right format, not string
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    
    out_df = fill_completion_failure(parse_events(df))
    out_df["timestamp"] = out_df["timestamp"].map(lambda x: x.isoformat())
    if "completed_at" in out_df.columns:
        out_df["completed_at"] = out_df["completed_at"].map(lambda x: x.isoformat())
    if "failed_at" in out_df.columns:
        out_df["failed_at"] = out_df["failed_at"].map(lambda x: x.isoformat())
    return out_df.sort_values(by="plugin_name")



def parse_events(df):
    v = []
    for _, row in df.iterrows():
        r = json.loads(row.value)
        r["timestamp"] = row.timestamp.isoformat()
        r["node"] = row["meta.node"]
        r["vsn"] = row["meta.vsn"]
        r["event"] = row["name"]
        v.append(r)
    return pd.read_json(json.dumps(v))


def fill_completion_failure(df):
    if len(df) == 0:
        return pd.DataFrame()
    # Filter only events related to plugin execution
    launched = df[df.event.str.contains("launched")]
    completed = df[df.event.str.contains("complete")]
    failed = df[df.event.str.contains("failed")]
    # launched.loc[launched["k3s_pod_name"] == completed["k3s_pod_name"]]
    for index, p in launched.iterrows():
        found = completed[completed.k3s_pod_instance == p.k3s_pod_instance]
        if len(found) > 0:
            launched.loc[index, "completed_at"] = found.iloc[0].timestamp
            launched.loc[index, "execution_time"] = (found.iloc[0].timestamp - p.timestamp).total_seconds()
            launched.loc[index, "k3s_pod_node_name"] = found.iloc[0].k3s_pod_node_name
            launched.loc[index, "end_state"] = "completed"
        else:
            found = failed[failed.k3s_pod_instance == p.k3s_pod_instance]
            if len(found) > 0:
                launched.loc[index, "failed_at"] = found.iloc[0].timestamp
                launched.loc[index, "execution_time"] = (found.iloc[0].timestamp - p.timestamp).total_seconds()
                launched.loc[index, "reason"] = found.iloc[0].reason
                if "error_log" in found.iloc[0]:
                    launched.loc[index, "error_log"] = found.iloc[0]["error_log"]
                launched.loc[index, "k3s_pod_node_name"] = found.iloc[0].k3s_pod_node_name
                launched.loc[index, "end_state"] = "failed"
            else:
                launched.loc[index, "end_state"] = "unknown"
    return launched


def calculate_cpu_utilization_from_cpuseconds(_df, new_t):
    new_row = _df.iloc[0]
    new_row.value = 0
    new_row.timestamp = new_t
    _df = pd.concat([new_row.to_frame().T, _df], ignore_index=True)
    _df["timestamp"] = pd.to_datetime(_df["timestamp"])
    _df["elapsed"] = _df.timestamp.diff().dt.total_seconds().cumsum()
    _df["cpu"] = _df.value.astype(float).diff().fillna(0) / _df.timestamp.diff().dt.total_seconds() * 100.
    return _df.loc[1:]


def is_gpu_requested(plugin_instance_record: pd.Series):
    if plugin_instance_record.plugin_selector is np.nan:
        return False

    selector = json.loads(plugin_instance_record.plugin_selector)
    
    if "resource.gpu" in selector and selector["resource.gpu"] == "true":
        return True
    return False


def generate_metrics_from_instance(t: tqdm, run: pd.Series):
    instance = run.k3s_pod_instance
    device = convert_nodename_to_devicename(run.k3s_pod_node_name)
    gpu_required = is_gpu_requested(run)
    plugin_name = run.plugin_name
    vsn = run.vsn
    started = pd.to_datetime(run.timestamp)
    completed = pd.to_datetime(run.completed_at)
    extended_started = pd.to_datetime(run.timestamp) - pd.to_timedelta(1, unit='m')
    extended_completed = pd.to_datetime(run.completed_at) + pd.to_timedelta(1, unit='m')
    extended_started = extended_started.isoformat()
    extended_completed = extended_completed.isoformat()

    t.write(f'{instance}: Fetching data from cloud ranging from {extended_started} to {extended_completed}')
    perf_df = download_performance_data(vsn, extended_started, extended_completed)
    if len(perf_df) < 1:
        t.write(f'No record found for {instance}')
        return pd.DataFrame()

    container_perf_df = perf_df[perf_df["meta.container"]==plugin_name]
    container_cpu_perf_df = container_perf_df[container_perf_df["name"]=="container_cpu_usage_seconds_total"]
    t.write(f'{instance}: {len(container_cpu_perf_df)} CPU records found')
    if len(container_cpu_perf_df) < 1:
        cpu = pd.DataFrame([], columns=["timestamp", "cpu"])
        cpu["timestamp"] = pd.to_datetime(cpu["timestamp"], utc=True)
    else:
        cpu = calculate_cpu_utilization_from_cpuseconds(container_cpu_perf_df.copy(), started)[["timestamp", "cpu"]]
        cpu = cpu.sort_values(by="timestamp")

    container_mem_rss_perf_df = container_perf_df[container_perf_df["name"]=="container_memory_rss"]
    container_mem_workingset_perf_df = container_perf_df[container_perf_df["name"]=="container_memory_working_set_bytes"]
    t.write(f'{instance}: {len(container_mem_workingset_perf_df)} Memory workingset records found')
    if len(container_mem_workingset_perf_df) < 1:
        mem = pd.DataFrame([], columns=["timestamp", "mem"])
        mem["timestamp"] = pd.to_datetime(mem["timestamp"], utc=True)
    else:
        container_mem_workingset_perf_df["mem"] = container_mem_workingset_perf_df["value"].values + container_mem_rss_perf_df["value"].values
        mem = container_mem_workingset_perf_df[["timestamp", "mem"]]
    try:
        mem = mem.sort_values(by="timestamp")
        merged_instance = pd.merge_asof(cpu[["timestamp", "cpu"]], mem[["timestamp", "mem"]], on="timestamp")
    except ValueError as ex:
        t.write(f'{instance}: ERROR="{ex}" DATA={cpu}')
        return pd.DataFrame()

    if "meta.sensor" not in perf_df.columns:
        t.write(f'{instance}: meta.sensor field not found. Unable to retrive power measurements')
        t.write(f'{instance}: columns in the data are {perf_df.columns}')
        pow = pd.DataFrame([], columns=["timestamp", "sys_power", "cpugpu_power"])
        pow["timestamp"] = pd.to_datetime(pow["timestamp"], utc=True)
        merged_instance = pd.merge_asof(merged_instance, pow, on="timestamp")
    else:
        tegra_total_power = perf_df[(perf_df["name"] == "tegra_wattage_current_milliwatts") & (perf_df["meta.sensor"] == "vdd_in")]
        t.write(f'{instance}: {len(tegra_total_power)} tegra power metric records found')
        tegra_total_power = tegra_total_power.rename({"value": "sys_power"}, axis="columns")
        tegra_total_power = tegra_total_power.sort_values(by="timestamp")
        merged_instance = pd.merge_asof(merged_instance, tegra_total_power[["timestamp", "sys_power"]], on="timestamp")

        tegra_cpugpu_power = perf_df[(perf_df["name"] == "tegra_wattage_current_milliwatts") & (perf_df["meta.sensor"] == "vdd_cpu_gpu_cv")]
        t.write(f'{instance}: {len(tegra_total_power)} tegra cpugpu power metric records found')
        tegra_cpugpu_power = tegra_cpugpu_power.rename({"value": "cpugpu_power"}, axis="columns")
        tegra_cpugpu_power = tegra_cpugpu_power.sort_values(by="timestamp")
        merged_instance = pd.merge_asof(merged_instance, tegra_cpugpu_power[["timestamp", "cpugpu_power"]], on="timestamp")

    # Merging all metrics
    merged_instance["plugin_instance"] = instance
    merged_instance["device"] = device
    merged_instance["gpu_requested"] = gpu_required
    merged_instance['timestamp'] = merged_instance['timestamp'].map(lambda x: x.isoformat())
    t.write(f'{instance}: Generated {len(merged_instance)} records. Done.')
    return merged_instance


def convert_nodename_to_devicename(node_name: str) -> str:
    if "nx" in node_name:
        return "Jetson"
    elif "rpi" in node_name:
        return "RaspberryPi"


def convert_relativetime_to_absolutetime(relative_time: str):
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        value = int(relative_time[:-1])
        unit = relative_time[-1]
        if unit == "s":
            delta = datetime.timedelta(seconds=value)
        elif unit == "m":
            delta = datetime.timedelta(minutes=value)
        elif unit == "h":
            delta = datetime.timedelta(hours=value)
        elif unit == "d":
            delta = datetime.timedelta(days=value)
        else:
            raise Exception(f'The unit {unit} should be in ["s", "m", "h", "d"]')
        return now - delta, None
    except Exception as ex:
        return relative_time, ex


def parse_time(t: str):
    if t == "":
        return datetime.datetime.now(datetime.timezone.utc), None

    if t[-1] in ["s", "m", "h", "d"]:
        return convert_relativetime_to_absolutetime(t)
    
    try:
        return datetime.datetime.fromisoformat(t), None
    except ValueError as ex:
        return t, ex
