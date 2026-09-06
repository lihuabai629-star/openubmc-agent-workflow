#!/usr/bin/env python3
"""Serve the real Runtime with a target-free Adapter for model measurements."""
import argparse
import json
from pathlib import Path
import sys
import time

from runtime_measurement import FixtureBackend


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--trace',type=Path,required=True)
    args=parser.parse_args()
    sys.path.insert(0,str(args.source/'openubmc-target-runtime'))
    from openubmc_target_runtime import FilesystemBlobRepository, JsonRpcMcpEndpoint, RuntimeMcpService, SQLiteRuntimeRepository
    from openubmc_target_runtime.mcp import StdioMcpServer
    service=RuntimeMcpService(FixtureBackend(),context_repository=SQLiteRuntimeRepository(args.trace.with_suffix('.sqlite3')),
                              blob_repository=FilesystemBlobRepository(args.trace.with_suffix('.blobs')))
    class Endpoint(JsonRpcMcpEndpoint):
        def handle(self, message):
            started=time.perf_counter()
            response=super().handle(message)
            if message.get('method') in ('tools/list','tools/call'):
                with args.trace.open('a') as output:
                    output.write(json.dumps({'request':message,'response':response,'runtime_wall_seconds':time.perf_counter()-started})+'\n')
            return response
    try:
        StdioMcpServer(Endpoint(service,session_task_id='model-measurement')).serve()
    finally:
        service.close()


if __name__=='__main__': main()
