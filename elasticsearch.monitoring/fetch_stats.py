#!/usr/bin/env python
import json
import os
import datetime
import time
import sys
import logging

import click
import requests

working_dir = os.path.dirname(os.path.realpath(__file__))

def merge(one, two):
    cp = one.copy()
    cp.update(two)
    return cp

def color_to_level(color):
    return {
        'green': 0,
        'yellow': 1,
        'red': 2
    }.get(color, 3)

def lookup(data, selector):
    keys = selector.split('.')
    value = data
    while keys:
        value = value[keys.pop(0)]
    return value

def delete_path(data, selector):
    keys = selector.split('.')
    value = data
    while keys:
        k = keys.pop(0)
        if k not in value:
            return
        value = value[k]

def assert_http_status(response, expected_status_code=200):
    if response.status_code != expected_status_code:
        print response.url, response.json()
        raise Exception('Expected HTTP status code %d but got %d' % (expected_status_code, response.status_code))

cluster_uuid = None

# Elasticsearch cluster to monitor
elasticCluster = os.environ.get('ES_METRICS_CLUSTER_URL', 'http://localhost:9200/')
interval = int(os.environ.get('ES_METRICS_INTERVAL', '1'))

# Elasticsearch Cluster to Send metrics to
monitoringCluster = os.environ.get('ES_METRICS_MONITORING_CLUSTER_URL', 'http://localhost:9200/')
indexPrefix = os.environ.get('ES_METRICS_INDEX_NAME', 'monitoring-test')

def fetch_cluster_health(base_url='http://localhost:9200/'):
    utc_datetime = datetime.datetime.utcnow()
    response = requests.get(base_url + '_cluster/health')
    jsonData = response.json()
    jsonData['timestamp'] = str(utc_datetime.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z')
    jsonData['status_code'] = color_to_level(jsonData['status'])
    return [jsonData]

node_stats_to_collect = ["indices", "os", "process", "jvm", "thread_pool", "fs", "transport", "http", "script", "ingest"]
def fetch_nodes_stats(base_url='http://localhost:9200/'):
    response = requests.get(base_url + '_nodes/stats')
    r_json = response.json()
    cluster_name = r_json['cluster_name']

    metric_docs = []

    # we are opting to not use the timestamp as reported by the actual node
    # to be able to better sync the various metrics collected
    utc_datetime = datetime.datetime.utcnow()

    for node_id, node in r_json['nodes'].items():
        node_data = {
            "timestamp": str(utc_datetime.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'),
            "cluster_name": cluster_name,
            "cluster_uuid": cluster_uuid,
            "source_node": {
                "uuid": node_id,
                "host": node['host'],
                "transport_address": node['transport_address'],
                "ip": node['ip'],
                "name": node['name'],
                "attributes": {} # TODO do we want to bring anything here?
            },
        }
        node_data["node_stats"] = {
            "node_id": node_id,
            "node_master": 'master' in node['roles'],
            "node_roles": node['roles'],
            "mlockall": True, # TODO here for compat reasons only
        }

        for k in node_stats_to_collect:
            node_data["node_stats"][k] = node[k]

        # clean up some stuff
        delete_path(node_data["node_stats"], "os.timestamp")
        del node_data["node_stats"]["process"]["timestamp"]
        del node_data["node_stats"]["os"]["timestamp"]
        del node_data["node_stats"]["jvm"]["timestamp"]
        del node_data["node_stats"]["jvm"]["mem"]["pools"]
        del node_data["node_stats"]["jvm"]["buffer_pools"]
        del node_data["node_stats"]["jvm"]["classes"]
        del node_data["node_stats"]["jvm"]["uptime_in_millis"]
        del node_data["node_stats"]["indices"]["segments"]["file_sizes"]
        # TODO remove some thread pools stats
        del node_data["node_stats"]["fs"]["timestamp"]
        del node_data["node_stats"]["fs"]["data"]
        del node_data["node_stats"]["ingest"]["pipelines"]

        metric_docs.append(node_data)

    return metric_docs

def fetch_index_stats():
    # TODO
    pass

def create_templates():
    # TODO verify and apply index templates here
    for filename in os.listdir(os.path.join(working_dir, 'templates')):
        if filename.endswith(".json"):
            with open(os.path.join(working_dir, 'templates', filename)) as query_base:
                template = query_base.read()
                template = template.replace('{{INDEX_PREFIX}}', indexPrefix + '*').strip()
                # print template
                templates_response = requests.put(monitoringCluster + '_template/' + filename[:-5], data = template)
                assert_http_status(templates_response)

def main():
    utc_datetime = datetime.datetime.utcnow()
    index_name = indexPrefix + str(utc_datetime.strftime('%Y.%m.%d'))

    cluster_health = fetch_cluster_health(elasticCluster)
    cluster_health_data = ['{"index":{"_index":"'+index_name+'","_type":"cluster_health"}}\n' + json.dumps(o)+'\n' for o in cluster_health]
    # TODO generate cluster_state documents

    node_stats = fetch_nodes_stats(elasticCluster)
    node_stats_data = ['{"index":{"_index":"'+index_name+'","_type":"node_stats"}}\n' + json.dumps(o)+'\n' for o in node_stats]
    data = node_stats_data + cluster_health_data
    bulk_response = requests.post(monitoringCluster + index_name + '/_bulk', data = '\n'.join(data))
    assert_http_status(bulk_response)
    for item in bulk_response.json()["items"]:
        if item.get("index") and item.get("index").get("status") != 201:
            print json.dumps(item.get("index").get("error"))

if __name__ == "__main__":
    # TODO use click
    response = requests.get(elasticCluster)
    assert_http_status(response)
    cluster_uuid = response.json()['cluster_uuid']
    create_templates()

    recurring = True
    if not recurring:
        main()
    else:
        try:
            nextRun = 0
            while True:
                if time.time() >= nextRun:
                    nextRun = time.time() + interval
                    now = time.time()
                    main()
                    elapsed = time.time() - now
                    print "Total Elapsed Time: %s" % elapsed
                    timeDiff = nextRun - time.time()

                    # Check timediff , if timediff >=0 sleep, if < 0 send metrics to es
                    if timeDiff >= 0:
                        time.sleep(timeDiff)

        except KeyboardInterrupt:
            print 'Interrupted'
            try:
                sys.exit(0)
            except SystemExit:
                os._exit(0)
