# Copyright (c) 2020 Seagate Technology LLC and/or its Affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# For any questions about this software or licensing,
# please email opensource@seagate.com or cortx-questions@seagate.com.
#

import asyncio
import base64
import logging
import os
import ssl
import sys
import json
from json.decoder import JSONDecodeError
from queue import Queue
from typing import Any, Callable, Dict, List, Type, Union, Optional

from aiohttp import web
from aiohttp.web import HTTPError, HTTPNotFound
from aiohttp.web_response import json_response

from hax.message import (BaseMessage, BroadcastHAStates, SnsDiskAttach,
                         SnsDiskDetach, SnsRebalancePause, SnsRebalanceResume,
                         SnsRebalanceStart, SnsRebalanceStatus,
                         SnsRebalanceStop, SnsRepairPause, SnsRepairResume,
                         SnsRepairStart, SnsRepairStatus, SnsRepairStop)
from hax.common import HaxGlobalState
from hax.motr.delivery import DeliveryHerald
from hax.exception import HAConsistencyException
from hax.motr import Motr
from hax.motr.planner import WorkPlanner
from hax.queue import BQProcessor
from hax.queue.confobjutil import ConfObjUtil
from hax.queue.offset import InboxFilter, OffsetStorage
from hax.types import Fid, HAState, ObjHealth, StoppableThread
from hax.util import ConsulUtil, create_process_fid, dump_json
from helper.exec import Executor, Program
from hax.util import repeat_if_fails
from hax.ha.utils import HaUtils
LOG = logging.getLogger('hax')


async def hello_reply(request):
    return json_response(text="I'm alive! Sincerely, HaX")


def get_python_env():
    path = os.environ['PATH']
    py_path = ':'.join(sys.path)
    if 'PYTHONPATH' in os.environ:
        env_var = os.environ['PYTHONPATH']
        py_path = f'{env_var}:{py_path}'
    env = {
        'PATH':
        ':'.join([
            '/opt/seagate/cortx/hare/bin',
            '/opt/seagate/cortx/hare/libexec', path
        ]),
        'PYTHONPATH':
        py_path
    }
    return env


def bytecount_stat(request):
    """
    Calls hare-status to provide CSM with --bytecount data in json format.

    This function calls for hare-status script from hax-server in order to
    provide CSM with an endpoint to get --byecount data in json format.
    """
    exec = Executor()
    env = get_python_env()
    result = exec.run(Program(["/opt/seagate/cortx/hare/libexec/hare-status",
                      "--bytecount"]), env=env)
    return json_response(text=result)


def hctl_stat(request):
    """
    Calls hare-status to provide CSM with hctl status --json data.

    This function calls the hare-status script from the hax-server in order
    to provide CSM with an endpoint to get the hctl status --json data.
    """
    exec = Executor()
    env = get_python_env()
    result = exec.run(Program(["/opt/seagate/cortx/hare/libexec/hare-status",
                      "--json"]), env=env)
    return json_response(text=result)


# This function implements the http fetch-fids request and thus, takes
# request a parameter. The respective result is obtained by executing
# corresponding `hctl-fetch-fids` script.
def hctl_fetch_fids(request):
    """calls the hare-fetch-fids to provide info about services configured.

    This function calls the hare-fetch-fids script from the hax-server in
    order to provide details about services configured by Hare e.g. Hax,
    ios, confd, rgw etc.
    """
    exec = Executor()
    env = get_python_env()
    result = exec.run(Program(["/opt/seagate/cortx/hare/libexec/"
                               "hare-fetch-fids",
                               "--all",
                               "--use-kv-store"]),
                      env=env)
    return json_response(text=result)


def to_ha_states(data: Any, consul_util: ConsulUtil) -> List[HAState]:
    """
    converts dictionary into list of HA states

    Converts a dictionary, obtained from JSON data, into a list of
    HA states.

    Format of an HA state: HAState(fid= <service fid>, status= <state>),
    where <state> is either 'online' or 'offline'.
    """
    if not data:
        return []

    ha_states = []
    for node in data:
        svc_status = ObjHealth.OK
        for check in node['Checks']:
            if check.get('Status') != 'passing':
                svc_status = ObjHealth.OFFLINE
            svc_id = check.get('ServiceID')
            if svc_id:
                ha_states.append(HAState(
                    fid=create_process_fid(int(svc_id)),
                    status=svc_status))
    LOG.debug('Reporting ha states: %s', ha_states)
    return ha_states


def process_ha_states(planner: WorkPlanner, consul_util: ConsulUtil):
    async def _process(request):
        data = await request.json()

        loop = asyncio.get_event_loop()

        def fn():
            # import pudb.remote
            # pudb.remote.set_trace(term_size=(80, 40), port=9998)
            LOG.debug('Service health from Consul: %s', data)
            planner.add_command(
                BroadcastHAStates(states=to_ha_states(data, consul_util),
                                  reply_to=None))

        # Note that planner.add_command is potentially a blocking call
        await loop.run_in_executor(None, fn)
        return web.Response()

    return _process


def process_sns_operation(planner: WorkPlanner):
    async def _process(request):
        op_name = request.match_info.get('operation')

        def create_handler(
            a_type: Callable[[Fid], BaseMessage]
        ) -> Callable[[Dict[str, Any]], BaseMessage]:
            def fn(data: Dict[str, Any]):
                fid = Fid.parse(data['fid'])
                return a_type(fid)

            return fn

        msg_factory = {
            'rebalance-start': create_handler(SnsRebalanceStart),
            'rebalance-stop': create_handler(SnsRebalanceStop),
            'rebalance-pause': create_handler(SnsRebalancePause),
            'rebalance-resume': create_handler(SnsRebalanceResume),
            'repair-start': create_handler(SnsRepairStart),
            'repair-stop': create_handler(SnsRepairStop),
            'repair-pause': create_handler(SnsRepairPause),
            'repair-resume': create_handler(SnsRepairResume),
            'disk-attach': create_handler(SnsDiskAttach),
            'disk-detach': create_handler(SnsDiskDetach),
        }

        LOG.debug(f'process_sns_operation: {op_name}')
        if op_name not in msg_factory:
            raise HTTPNotFound()
        data = await request.json()
        message = msg_factory[op_name](data)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: planner.add_command(message))
        return web.Response()

    return _process


def get_sns_status(planner: WorkPlanner,
                   status_type: Union[Type[SnsRepairStatus],
                                      Type[SnsRebalanceStatus]]):
    def fn(request):
        queue: Queue = Queue(1)
        planner.add_command(
            status_type(reply_to=queue,
                        fid=Fid.parse(request.query['pool_fid'])))
        return queue.get(timeout=10)

    async def _process(request):
        LOG.debug('%s with params: %s', request, request.query)
        loop = asyncio.get_event_loop()
        payload = await loop.run_in_executor(None, fn, request)
        return json_response(data=payload, dumps=dump_json)

    return _process


def process_bq_update(inbox_filter: InboxFilter, processor: BQProcessor):
    async def _process(request):
        data = await request.json()

        def fn():
            messages = inbox_filter.prepare(data)
            if not messages:
                return
            for i, msg in messages:
                processor.process((i, msg))
                # Mark the message as read ASAP since the process can
                # potentially die any time
                inbox_filter.offset_mgr.mark_last_read(i)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, fn)

        return web.Response()

    return _process


def process_state_update(planner: WorkPlanner):
    async def _process(request):
        data = await request.json()

        loop = asyncio.get_event_loop()

        def fn():
            proc_state_to_objhealth = {
                'M0_CONF_HA_PROCESS_STARTING': ObjHealth.OFFLINE,
                'M0_CONF_HA_PROCESS_STARTED': ObjHealth.RECOVERING,
                'M0_CONF_HA_PROCESS_DTM_RECOVERED': ObjHealth.OK,
                'M0_CONF_HA_PROCESS_STOPPING': ObjHealth.OFFLINE,
                'M0_CONF_HA_PROCESS_STOPPED': ObjHealth.OFFLINE
            }
            # import pudb.remote
            # pudb.remote.set_trace(term_size=(80, 40), port=9998)
            ha_states: List[HAState] = []
            LOG.debug('process status: %s', data)
            proc_status_val = base64.b64decode(data['Value']).decode("utf-8")
            proc_status = json.loads(proc_status_val)
            LOG.debug('process status %s', proc_status)
            proc_fid = Fid.parse(data['Key'].split('/')[1])
            proc_state = proc_status['state']
            proc_type = proc_status['type']
            if (proc_type != 'M0_CONF_HA_PROCESS_M0MKFS' and
                proc_state in ('M0_CONF_HA_PROCESS_STARTED',
                               'M0_CONF_HA_PROCESS_DTM_RECOVERED',
                               'M0_CONF_HA_PROCESS_STOPPED')):
                LOG.debug('Adding item key %d item val: %s',
                          proc_fid.key, proc_status)
                ha_states.append(HAState(
                    fid=proc_fid,
                    status=proc_state_to_objhealth[proc_state]))
                planner.add_command(
                    BroadcastHAStates(states=ha_states, reply_to=None))
        # Note that planner.add_command is potentially a blocking call
        try:
            await loop.run_in_executor(None, fn)
        except Exception:
            LOG.exception("process state update error")
        return web.Response()

    return _process


def event_subscription_handle(consul_util: ConsulUtil):
    async def _process(request):
        data = await request.json()

        loop = asyncio.get_event_loop()

        try:
            ha_util = HaUtils(consul_util)
            await loop.run_in_executor(None, ha_util.event_subscribe, data)
        except Exception as e:
            LOG.exception(f'Event subscribe error: {e}')
            return web.Response(text=f'Event subscribe error: {e}')
        return web.Response()

    return _process


def event_unsubscription_handle(consul_util: ConsulUtil):
    async def _process(request):
        data = await request.json()

        loop = asyncio.get_event_loop()

        try:
            ha_util = HaUtils(consul_util)
            await loop.run_in_executor(None, ha_util.event_unsubscribe, data)
        except Exception as e:
            LOG.exception(f'Event unsubscribe error: {e}')
            return web.Response(text=f'Event unsubscribe error: {e}')
        return web.Response()

    return _process


@web.middleware
async def encode_exception(request, handler):
    def error_response(e: Exception, code=500, reason=""):
        payload = {
            "status_code": code,
            "error_message": str(e),
            "error_type": e.__class__.__name__,
            "reason": reason
        }
        return json_response(data=payload, status=code)

    try:
        response = await handler(request)
        return response
    except HTTPError:
        raise
    except (JSONDecodeError, KeyError) as e:
        return error_response(e, code=400, reason="Bad JSON provided")
    except Exception as e:
        return error_response(e,
                              code=500,
                              reason="Unexpected error has happened")


class ServerRunner:
    def __init__(
        self,
        planner: WorkPlanner,
        herald: DeliveryHerald,
        motr: Motr,
        consul_util: ConsulUtil,
        hax_state: HaxGlobalState
    ):
        self.consul_util = consul_util
        self.herald = herald
        self.motr = motr
        self.planner = planner
        self.hax_state = hax_state

    def _create_server(self) -> web.Application:
        return web.Application(middlewares=[encode_exception])

    def _get_my_hostname(self) -> str:
        hax_hostname: str = self.consul_util.get_hax_hostname()
        return hax_hostname

    @repeat_if_fails()
    def _configure(self) -> None:
        try:
            # We can't use broad 0.0.0.0 IP address to make it possible to run
            # multiple hax instances at the same machine (i.e. in failover
            # situation).
            # Instead, every hax will use a private IP only.
            node_address = self._get_my_hostname()

            # Note that bq-delivered mechanism must use a unique
            # node name rather than broad '0.0.0.0' that doesn't
            # identify the node from outside.
            inbox_filter = InboxFilter(
                OffsetStorage(node_address,
                              key_prefix='bq-delivered',
                              kv=self.consul_util.kv))

            conf_obj = ConfObjUtil(self.consul_util)
            planner = self.planner
            herald = self.herald
            motr = self.motr
            consul_util = self.consul_util

            app = self._create_server()
            app.add_routes([
                web.get('/', hello_reply),
                web.get('/v1/cluster/status', hctl_stat),
                web.get('/v1/cluster/status/bytecount', bytecount_stat),
                web.get('/v1/cluster/fetch-fids', hctl_fetch_fids),
                web.post('/', process_ha_states(planner, consul_util)),
                web.post(
                    '/watcher/bq',
                    process_bq_update(inbox_filter,
                                      BQProcessor(planner, herald, motr,
                                                  conf_obj))),
                web.post(
                    '/watcher/processes',
                    process_state_update(planner)),
                web.post('/api/v1/sns/{operation}',
                         process_sns_operation(planner)),
                web.get('/api/v1/sns/repair-status',
                        get_sns_status(planner, SnsRepairStatus)),
                web.get('/api/v1/sns/rebalance-status',
                        get_sns_status(planner, SnsRebalanceStatus)),
                web.post('/v1/events/subscribe',
                         event_subscription_handle(consul_util)),
                web.post('/v1/events/unsubscribe',
                         event_unsubscription_handle(consul_util)),
            ])
            self.app = app
        except Exception as e:
            raise HAConsistencyException('Failed to configure hax') from e

    def _get_ssl_context(self) -> Optional[ssl.SSLContext]:
        ssl_config = self.consul_util.get_hax_ssl_config()
        if not ssl_config or ssl_config.get('http_protocol') != "https":
            return None

        certificate_path = ssl_config.get('cert_path', '')
        private_key_path = ssl_config.get('key_path', '')
        if not all(map(os.path.exists,
                       (certificate_path, private_key_path))):
            LOG.warning("Invalid path to certificate/private key. "
                        "Fallback to HTTP")
            return None

        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(certificate_path, private_key_path)
        return ssl_context

    @repeat_if_fails()
    def _start(self, port: int) -> None:
        try:
            web_address = self._get_my_hostname()
            ssl_context = self._get_ssl_context()
            LOG.info(f'Starting HTTP{ssl_context and "S" or ""} server at '
                     f'{web_address}:{port} ...')
            web.run_app(self.app, host=web_address, port=port,
                        ssl_context=ssl_context)
        except Exception as e:
            raise HAConsistencyException(
                'Failed to start web server, trying again...') from e

    @repeat_if_fails()
    def run(
        self,
        threads_to_wait: List[StoppableThread] = [],
        port=8008,
    ):
        self._configure()
        try:
            self._start(port)
            LOG.debug('Server stopped normally')
        except Exception as e:
            raise HAConsistencyException(
                'Failed to start web server, trying again...') from e
        finally:
            self.hax_state.set_stopping()
            LOG.debug('Stopping the threads')
            self.planner.shutdown()
            for thread in threads_to_wait:
                thread.stop()
            for thread in threads_to_wait:
                thread.join()

            LOG.info('The http server has stopped')
