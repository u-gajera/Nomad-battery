import logging

import pytest
from multiprocessing import Process
import socket
import time

from nomad.config import config
from nomad import logtransfer


logging.getLogger('logtransfer_server').setLevel(level=logging.CRITICAL)


def start_logtransfer_service():
    # Change address such that api_v1 fixture can replace the address to the testserver
    config.oasis.central_nomad_deployment_url = config.client.url + '/v1'

    # Use a different port to the logstash mock (which uses 5000 by default)
    host, port = config.logstash.host, int(config.logstash.tcp_port) + 1

    server_process = Process(
        target=logtransfer.start_logtransfer_service,
        kwargs=dict(host=host, port=port),
        name='process_logtransfer',
    )

    server_process.daemon = True
    server_process.start()

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # NOTE: it is in the responsibility of the tests to timeout if no connection can be
    #  established (use @pytest.mark.timeout( SECONDS ))
    while True:
        time.sleep(0.001)
        try:
            client_socket.connect((host, port))
            break
        except ConnectionRefusedError:
            pass

    return server_process, client_socket


def kill_logtransfer_service(server_process, client_socket):
    client_socket.close()
    while server_process.is_alive():
        server_process.kill()

    # clear up files
    logtransfer.clear_logfiles()


@pytest.fixture(scope='function')
def logstash_enabled(monkeysession):
    monkeysession.setattr('nomad.config.logstash.enabled', True)

    # make sure the root logger has the logstash handler
    from nomad.utils.structlogging import add_logstash_handler, root

    add_logstash_handler(root)


@pytest.fixture(scope='function')
def logtransfer_rollover_time(api_v1, monkeysession):
    monkeysession.setattr('nomad.config.logtransfer.enable_statistics', False)
    monkeysession.setattr('nomad.config.logtransfer.submit_interval', 0.1)
    monkeysession.setattr('nomad.config.logtransfer.raise_unexpected_exceptions', True)

    server_process, client_socket = start_logtransfer_service()

    yield client_socket

    kill_logtransfer_service(server_process, client_socket)


@pytest.fixture(scope='function')
def logtransfer_rollover_space(api_v1, monkeysession):
    monkeysession.setattr('nomad.config.logtransfer.enable_statistics', False)
    monkeysession.setattr('nomad.config.logtransfer.max_bytes', 500)
    monkeysession.setattr('nomad.config.logtransfer.backup_count', 100)
    monkeysession.setattr('nomad.config.logtransfer.raise_unexpected_exceptions', True)

    server_process, client_socket = start_logtransfer_service()

    yield client_socket

    kill_logtransfer_service(server_process, client_socket)


@pytest.fixture(scope='function')
def logtransfer_no_rollover(api_v1, monkeysession):
    monkeysession.setattr('nomad.config.logtransfer.enable_statistics', False)
    monkeysession.setattr('nomad.config.logtransfer.max_bytes', 1e8)
    monkeysession.setattr('nomad.config.logtransfer.submit_interval', 1e8)
    monkeysession.setattr('nomad.config.logtransfer.raise_unexpected_exceptions', True)

    server_process, client_socket = start_logtransfer_service()

    yield client_socket

    kill_logtransfer_service(server_process, client_socket)


@pytest.fixture(scope='function')
def collect_statistics(monkeysession):
    monkeysession.setattr('nomad.config.logtransfer.statistics_interval', 1)
