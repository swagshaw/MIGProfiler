#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Author: Li Yuanming
Author: Yizheng Huang
Email: yli056@e.ntu.edu.sg
Email: huangyz0918@gmail.com
Date: 9/13/2020
Track all the SLO data performing NLP task from the lightweight distributed system under a simple workload.
The workload is a Poisson process with user specified arrival rate.
The collected metric is combine of client and server settings, timing information (end-to-end latency,
inference latency, etc.), monitor information (CPU utilization, GPU utilization, Memory, etc.):
The result will be saved into MongoDB collection. The collection default name is `test`. You can provide an argument
`-dbn` or `--database_name` to change the collection to save.
Examples:
    usage: slo_tracker_cv_no_scale.py [-h] -b BS -m MODEL [MODEL ...] [--url URL]
                                      [-n NAME] [-dbn DATABASE_NAME] [-r RATE]
                                      [-t TIME] [--text TEXT]
    optional arguments:
      -h, --help            show this help message and exit
      -b BS, --bs BS        frontend batch size
      -m MODEL [MODEL ...], --model MODEL [MODEL ...]
                            A list of names of the used models. For example,
                            resnet18.
      --url URL             The host url of your services. Default to
                            http://localhost:8000.
      -n NAME, --name NAME  The service name you are testing. Default to
                            image_classification.
      -dbn DATABASE_NAME, --database_name DATABASE_NAME
                            The database name you record data to. Default to test.
      -r RATE, --rate RATE  The arrival rate. Default to 5.
      -t TIME, --time TIME  The testing duration. Default to 30.
      --text TEXT           The path to your testing image. Default to
                            ${PROJECT_ROOT}/exp/data/img_bigbang_scene.jpg
Attributes:
    DATA_PATH: Path to the default images for testing.
    SEED: Seed for generating requests arriving follows a Poisson distribution.
Notes:
    You may experience the reported exception: [Errno 24] Too many open files.
    This exception raised probabiliy as a result of too little file open number. Check the number by:
    ````shell script
    ulimit -n
    ```
    Change a larger number:
    ```shell script
    ulimit -n 4096
    ```
"""
import argparse
import json
import pickle
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Thread

import numpy as np
import requests
# from tqdm import tqdm

from utils.dtype import type_to_data_type, serialize_byte_tensor, DataType
# from utils.logger import Printer
from utils.pipeline_manager import PreProcessor
# from workload.generator import WorkloadGenerator

DATA_PATH = str(Path(__file__).parent / 'n02124075_Egyptian_cat.jpg')
SEED = 666
start_time = 0
request_num = 0

results = set()

latency_list = []
send_time_list = []


# noinspection DuplicatedCode
def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-b', '--bs', help='frontend batch size', type=int, required=True)
    parser.add_argument('-m', '--model', type=str, nargs='+', required=True,
                        help='A list of names of the used models. For example, resnet18.')
    parser.add_argument('--url', type=str, default='http://localhost:50075',
                        help='The host url of your services. Default to http://localhost:50075.')
    parser.add_argument('-n', '--name', type=str, default='image_classification',
                        help='The service name you are testing. Default to image_classification.')
    parser.add_argument('-dbn', '--database_name', type=str, default='test',
                        help='The database name you record data to. Default to test.')
    parser.add_argument('-r', '--rate', help='The arrival rate. Default to 5.', type=float, default=5)
    parser.add_argument('-t', '--time', help='The testing duration. Default to 30.', type=float, default=30)
    parser.add_argument('--data', type=str, default=DATA_PATH,
                        help=f'The path to your testing image. Default to {DATA_PATH}')
    parser.add_argument('-P', '--preprocessing', action='store_true', help='Use client preprocessing.')
    return parser.parse_args()


def sender(url, request):
    global latency_list

    send_time = time.time()
    response = requests.post(url, **request)
    receive_time = time.time()
    assert response.status_code == 200
    result = response.json()
    response.close()
    latency = receive_time - send_time
    client_server_rtt = latency - result['times']['server_end2end_time']
    result['times'].update({
        'latency': latency,
        'client_server_rtt': client_server_rtt,
    })
    latency_list.append(latency)
    return result


def make_restful_request_from_numpy(input_tensor: np.ndarray):
    """Make the RESTful request here.

    Args:
        input_tensor (numpy.ndarray): The input tensor in numpy array format.
    """

    if not isinstance(input_tensor, (np.ndarray,)):
        raise ValueError('input_tensor must be a numpy array')
    datatype = type_to_data_type(input_tensor.dtype)

    content = {
        'shape': list(input_tensor.shape),
        'datatype': datatype.name
    }

    if datatype == DataType.TYPE_BYTES:
        content['raw_input_contents'] = serialize_byte_tensor(input_tensor).tobytes()
    else:
        content['raw_input_contents'] = input_tensor.tobytes()

    files = {'content': pickle.dumps(content)}

    return {'files': files}


def metric_collector(args):
    """Collect all metrics information from Redis."""
    while is_running:
        time.sleep(1)
        for model_name in args.model:
            aggr_stat_data = redis_client.hget(MONITOR_AGGREGATION_KEY, model_name)
            data_collected_time = time.time()
            if aggr_stat_data is not None:
                aggr_stat = AggregationStat()
                aggr_stat.ParseFromString(aggr_stat_data)
                for metric_name, value in json_format.MessageToDict(aggr_stat).items():
                    aggr_metric_result_dict[metric_name].append(value)
                aggr_metric_result_dict['time'].append(data_collected_time - start_time)

        # { ip: metric_protobuf }
        stat_data_dict = redis_client.hgetall(MONITOR_STAT_KEY)
        data_collected_time = time.time()
        # { metric_name: { ip: ... } }
        stat_dict = defaultdict(dict)
        for ip_bytes, stat_data in stat_data_dict.items():
            ip = ip_bytes.decode()
            stat = SysStat()
            stat.ParseFromString(stat_data)

            # remove unwanted metrics
            stat = json_format.MessageToDict(stat)
            del stat['ip']
            del stat['gpuCount']
            for metric_name, value in stat.items():
                stat_dict[metric_name][ip.replace('.', '[dot]')] = value

        for metric_name, value in stat_dict.items():
            metric_result_dict[metric_name].append(value)
        metric_result_dict['time'].append(data_collected_time - start_time)


def warm_up(args):
    """Warm up for 100 requests at 10ms each pre GPU worker"""
    url = f'{args.url}/predict'
    with open(args.data, 'rb') as f:
        image = f.read()
    image_np = np.frombuffer(image, dtype=np.uint8)
    if args.preprocessing:
        image_np = PreProcessor.transform_image2torch([image_np]).numpy()[0]
    request = make_restful_request_from_numpy(image_np)
    num = args.bs * 100

    with ThreadPoolExecutor(10) as executor:
        futures = set()
        for i in range(num):
            futures.add(executor.submit(sender, url, request))
            time.sleep(0.01)
        for future in as_completed(futures):
            future.result()


def send_stress_test_data(args):
    """
    send stress testing data.
    """
    global start_time, request_num, send_time_list

    arrival_rate = args.rate
    duration = args.time
    url = f'{args.url}/predict/{args.name}'
    with open(args.data, 'rb') as f:
        image = f.read()
    image_np = np.frombuffer(image, dtype=np.uint8)
    if args.preprocessing:
        image_np = PreProcessor.transform_image2torch([image_np]).numpy()[0]
    request = make_restful_request_from_numpy(image_np)

    send_time_list = WorkloadGenerator.gen_arrival_time(
        duration=duration, arrival_rate=arrival_rate, seed=SEED
    )

    # cut list to a multiple of <BATCH_SIZE>, so that the light-weight system can do full batch prediction
    request_num = len(send_time_list) // args.bs * args.bs
    print(f'Generating {request_num} exadmples')

    start_time = time.time()
    metric_collector_thread.start()

    with ThreadPoolExecutor(10) as executor:
        for arrive_time in tqdm(send_time_list[:request_num]):
            results.add(executor.submit(sender, url, request))
            time.sleep(max(arrive_time + start_time - time.time(), 0))


def process_result(args):
    timing_metric_names = [
        'latency', 'client_server_rtt', # 'batching_time',
        'inference_time', 'postprocessing_time'
    ]
    if args.preprocessing:
        timing_metric_names.append('preprocessing_time')
    timing_metric_raw_result_dict = defaultdict(list)
    timing_metric_aggr_result_dict = dict()

    raw_result = list()
    fail_count = 0
    for future in as_completed(results):
        try:
            raw_result.append(future.result()['times'])
        except Exception:
            fail_count += 1
            print('.', end='')
            if fail_count % 20 == 0:
                print()

    finish_time = time.time()

    for result in raw_result:
        for metric_name in timing_metric_names:
            timing_metric_raw_result_dict[metric_name].append(result[metric_name])

    for metric_name, raw_result in timing_metric_raw_result_dict.items():
        mean = np.mean(raw_result)
        std = np.std(raw_result)
        p50 = np.percentile(raw_result, 50)
        p95 = np.percentile(raw_result, 95)
        p99 = np.percentile(raw_result, 99)

        timing_metric_aggr_result_dict[metric_name + '_mean'] = mean
        timing_metric_aggr_result_dict[metric_name + '_std'] = std
        timing_metric_aggr_result_dict[metric_name + '_p50'] = p50
        timing_metric_aggr_result_dict[metric_name + '_p95'] = p95
        timing_metric_aggr_result_dict[metric_name + '_p99'] = p99

    # report
    print(f'Failing test number: {fail_count}')

    device_dict = dict()
    for url, init_replica_config_dict in config.frontend.replicas.items():
        ip = url.host
        devices = set()
        for init_replica_configs in init_replica_config_dict.values():
            devices.update([replica_config.device for replica_config in init_replica_configs])
        device_dict[ip] = list(devices)

    result = {
        'test_time': str(datetime.now()),
        'arrival_rate': args.rate, 'testing_time': args.time,
        'batch_size': args.bs, 'latency_list': latency_list, 'time_list': send_time_list,
        'default_model': args.model, 'service_name': args.name,
        'load_balancer': config.frontend.load_balancer,
        'fail_count': fail_count, 'qps': request_num / (finish_time - start_time),
        'devices': json.dumps(device_dict), 'servers': list(device_dict.keys()),
        'client_preprocessing': args.preprocessing, 'config': config.export_json()
    }

    result.update(timing_metric_raw_result_dict)
    result.update(timing_metric_aggr_result_dict)
    result['metrics'] = metric_result_dict
    result['aggr_metrics'] = aggr_metric_result_dict

    # save the experiment records to the database and print to the console.
    # TODO: change the database settings to the configuration file.
    # TODO: note that you need to change doc_name
    Printer.add_record_to_database(result, db_name='ml_cloud_autoscaler',
                                   address="mongodb://mongodb.withcap.org:27127/",
                                   doc_name=args.database_name)


if __name__ == '__main__':
    args_ = get_args()
    # redis_client = RedisClient()
    metric_result_dict = defaultdict(list)
    aggr_metric_result_dict = defaultdict(list)
    metric_collector_thread = Thread(target=metric_collector, args=(args_,))
    # is_running = True

    print('Testing on:')
    print(f'arrival rate: {args_.rate};', f'testing time: {args_.time};')
    print(f'batch size: {args_.bs};', f'model name: {args_.model}')

    print('Warming up...')
    warm_up(args_)
    print('Testing...')
    # send_stress_test_data(args_)
    print('Finish')
    is_running = False  # noqa
    # metric_collector_thread.join()

    # process_result(args_)
    # redis_client.close()