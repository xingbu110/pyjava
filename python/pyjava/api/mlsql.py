import os
import socket
import sys
import uuid

import pandas as pd

import pyjava.utils as utils
from pyjava.serializers import ArrowStreamSerializer
from pyjava.serializers import read_int
from pyjava.utils import utf8_deserializer

if sys.version >= '3':
    basestring = str
else:
    pass


class DataServer(object):
    def __init__(self, host, port, timezone):
        self.host = host
        self.port = port
        self.timezone = timezone


class LogClient(object):
    def __init__(self, conf):
        self.conf = conf
        if 'spark.mlsql.log.driver.host' in self.conf:
            self.log_host = self.conf['spark.mlsql.log.driver.host']
            self.log_port = self.conf['spark.mlsql.log.driver.port']
            self.log_user = self.conf['PY_EXECUTE_USER']
            self.log_token = self.conf['spark.mlsql.log.driver.token']
            self.log_group_id = self.conf['groupId']
            import socket
            self.conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.conn.connect((self.log_host, int(self.log_port)))
            buffer_size = int(os.environ.get("SPARK_BUFFER_SIZE", 256))
            self.infile = os.fdopen(
                os.dup(self.conn.fileno()), "rb", buffer_size)
            self.outfile = os.fdopen(
                os.dup(self.conn.fileno()), "wb", buffer_size)

    def log_to_driver(self, msg):
        if not self.log_host:
            print(msg)
            return
        from pyjava.serializers import write_bytes_with_length
        import json
        resp = json.dumps(
            {"sendLog": {
                "token": self.log_token,
                "logLine": "[owner] [{}] [groupId] [{}] {}".format(self.log_user, self.log_group_id, msg)
            }}, ensure_ascii=False)
        write_bytes_with_length(resp, self.outfile)

    def close(self):
        if hasattr(self, "conn"):
            self.conn.close()
            self.conn = None


class PythonContext(object):
    cache = {}

    def __init__(self, iterator, conf):
        self.input_data = iterator
        self.output_data = [[]]
        self.conf = conf
        self.schema = ""
        self.have_fetched = False
        self.saved_file = None
        self.log_client = LogClient(self.conf)
        if "pythonMode" in conf and conf["pythonMode"] == "ray":
            self.rayContext = RayContext(self)

    def set_output(self, value, schema=""):
        self.output_data = value
        self.schema = schema

    @staticmethod
    def build_chunk_result(items, block_size=1024):
        buffer = []
        for item in items:
            buffer.append(item)
            if len(buffer) == block_size:
                df = pd.DataFrame(buffer, columns=buffer[0].keys())
                buffer.clear()
                yield df

        if len(buffer) > 0:
            df = pd.DataFrame(buffer, columns=buffer[0].keys())
            buffer.clear()
            yield df

    def build_result(self, items, block_size=1024):
        self.output_data = ([df[name] for name in df]
                            for df in PythonContext.build_chunk_result(items, block_size))

    def output(self):
        return self.output_data

    def __del__(self):
        self.log_client.close()
        if self.saved_file and os.exists(self.saved_file):
            os.remove(self.saved_file)

    def noops_fetch(self):
        for item in self.fetch_once():
            pass

    def fetch_once_as_dataframe(self):
        for df in self.fetch_once():
            yield df

    def fetch_once_as_rows(self):
        for df in self.fetch_once_as_dataframe():
            for row in df.to_dict('records'):
                yield row

    def fetch_once_as_batch_rows(self):
        for df in self.fetch_once_as_dataframe():
            yield (row for row in df.to_dict('records'))

    def fetch_once(self):
        import pyarrow as pa
        if self.have_fetched:
            raise Exception("input data can only be fetched once")
        self.have_fetched = True
        for items in self.input_data:
            yield pa.Table.from_batches([items]).to_pandas()

    def fetch_as_file(self):
        import pyarrow as pa
        if not self.saved_file:
            self.save_as_file()
            for items in self.input_data:
                yield pa.Table.from_batches([items]).to_pandas()
        else:
            out_ser = ArrowStreamSerializer()
            buffer_size = int(os.environ.get("BUFFER_SIZE", 65536))
            import pyarrow.fs as pa_fs
            local_file_system = pa_fs.LocalFileSystem()
            with local_file_system.open_input_stream(self.saved_file, buffer_size) as fs:
                result = out_ser.load_stream(fs)
                for items in result:
                    yield pa.Table.from_batches([items]).to_pandas()

    def save_as_file(self):
        in_ser = ArrowStreamSerializer()
        buffer_size = int(os.environ.get("BUFFER_SIZE", 65536))
        file = str(self.conf["tempDataLocalPath"]).join('\\').join(str(uuid.uuid1()).join('.pyjava'))
        self.saved_file = file
        import pyarrow.fs as pa_fs
        local_file_system = pa_fs.LocalFileSystem()
        with local_file_system.open_output_stream(file, buffer_size) as fs:
            in_ser.dump_stream(self.input_data, fs)


class PythonProjectContext(object):
    def __init__(self):
        self.params_read = False
        self.conf = {}
        self.read_params_once()
        self.log_client = LogClient(self.conf)

    def read_params_once(self):
        if not self.params_read:
            self.params_read = True
            infile = sys.stdin.buffer
            for i in range(read_int(infile)):
                k = utf8_deserializer.loads(infile)
                v = utf8_deserializer.loads(infile)
                self.conf[k] = v

    def input_data_dir(self):
        return self.conf["tempDataLocalPath"]

    def output_model_dir(self):
        return self.conf["tempModelLocalPath"]

    def __del__(self):
        self.log_client.close()


class RayContext(object):
    cache = {}

    def __init__(self, python_context):
        self.python_context = python_context
        self.servers = []
        self.server_ids_in_ray = []
        self.is_setup = False
        self.rds_list = []
        self.is_dev = utils.is_dev()
        self.is_in_mlsql = True
        self.mock_data = []
        for item in self.python_context.fetch_once_as_rows():
            self.server_ids_in_ray.append(str(uuid.uuid4()))
            self.servers.append(DataServer(
                item["host"], int(item["port"]), item["timezone"]))

    def data_servers(self):
        return self.servers

    def data_servers_in_ray(self):
        import ray
        for server_id in self.server_ids_in_ray:
            server = ray.experimental.get_actor(server_id)
            yield ray.get(server.connect_info.remote())

    def build_servers_in_ray(self):
        import ray
        from pyjava.api.serve import RayDataServer
        buffer = []
        for (server_id, java_server) in zip(self.server_ids_in_ray, self.servers):

            rds = RayDataServer.options(name=server_id, detached=True, max_concurrency=2).remote(server_id, java_server,
                                                                                                 0,
                                                                                                 java_server.timezone)
            self.rds_list.append(rds)
            res = ray.get(rds.connect_info.remote())
            if self.is_dev:
                print("build RayDataServer server_id:{} java_server: {} servers:{}".format(server_id,
                                                                                           str(vars(
                                                                                               java_server)),
                                                                                           str(vars(res))))
            buffer.append(res)
        return buffer

    @staticmethod
    def connect(_context, url):

        if isinstance(_context, PythonContext):
            context = _context
        elif isinstance(_context, dict):
            if 'context' in _context:
                context = _context['context']
            else:
                '''
                we are not in MLSQL
                '''
                context = PythonContext([], {"pythonMode": "ray"})
                context.rayContext.is_in_mlsql = False
        else:
            raise Exception("context is not set")

        import ray
        ray.shutdown(exiting_interpreter=False)
        ray.init(redis_address=url)
        return context.rayContext

    def setup(self, func_for_row, func_for_rows=None):
        if self.is_setup:
            raise ValueError("setup can be only invoke once")
        self.is_setup = True
        import ray

        if not self.is_in_mlsql:
            if func_for_rows is not None:
                func = ray.remote(func_for_rows)
                return ray.get(func.remote(self.mock_data))
            else:
                func = ray.remote(func_for_row)

                def iter_all(rows):
                    return [ray.get(func.remote(row)) for row in rows]

                iter_all_func = ray.remote(iter_all)
                return ray.get(iter_all_func.remote(self.mock_data))

        buffer = []
        for server_info in self.build_servers_in_ray():
            server = ray.experimental.get_actor(server_info.server_id)
            buffer.append(ray.get(server.connect_info.remote()))
            server.serve.remote(func_for_row, func_for_rows)
        items = [vars(server) for server in buffer]
        self.python_context.build_result(items, 1024)
        return buffer

    def foreach(self, func_for_row):
        return self.setup(func_for_row)

    def map_iter(self, func_for_rows):
        return self.setup(None, func_for_rows)

    def collect(self):
        for shard in self.data_servers():
            for row in RayContext.fetch_once_as_rows(shard):
                yield row

    @staticmethod
    def collect_from(servers):
        for shard in servers:
            for row in RayContext.fetch_once_as_rows(shard):
                yield row

    def to_pandas(self):
        items = [row for row in self.collect()]
        return pd.DataFrame(data=items)

    @staticmethod
    def fetch_once_as_rows(data_server):
        for df in RayContext.fetch_data_from_single_data_server(data_server):
            for row in df.to_dict('records'):
                yield row

    @staticmethod
    def fetch_data_from_single_data_server(data_server):
        out_ser = ArrowStreamSerializer()
        import pyarrow as pa
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect((data_server.host, data_server.port))
            buffer_size = int(os.environ.get("BUFFER_SIZE", 65536))
            infile = os.fdopen(os.dup(sock.fileno()), "rb", buffer_size)
            result = out_ser.load_stream(infile)
            for items in result:
                yield pa.Table.from_batches([items]).to_pandas()
