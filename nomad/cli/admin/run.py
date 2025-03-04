#
# Copyright The NOMAD Authors.
#
# This file is part of NOMAD. See https://nomad-lab.eu for further info.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import functools
import click

from nomad import utils
from nomad.config import config

from .admin import admin


@admin.group(help='Run a nomad service locally (outside docker).')
def run():
    pass


@run.command(help='Run the jupyter hub.')
def hub():
    run_hub()


@run.command(help='Run the nomad development worker.')
def worker():
    run_worker()


@run.command(help='Run the nomad development app with all apis.')
@click.option(
    '--with-gui',
    help='The app will configure the gui for production and service it.',
    is_flag=True,
)
@click.option('--host', type=str, help='Passed as host parameter.')
@click.option('--port', type=int, help='Passed as port parameter.')
@click.option('--log-config', type=str, help='Passed as log-config parameter.')
@click.option(
    '--gunicorn',
    is_flag=True,
    type=bool,
    help='Run app with gunicorn instead of uvicorn.',
)
@click.option('--workers', type=int, help='Passed to uvicorn workers parameter.')
def app(with_gui: bool, **kwargs):
    run_app(with_gui=with_gui, **kwargs)


@run.command(
    help='Run service to collect and submits logs to the central Nomad instance.'
)
def logtransfer():
    config.meta.service = 'logtransfer'

    from nomad.logtransfer import start_logtransfer_service
    from nomad.statistics_logger import start_statistics_logger_process

    if not config.logstash.enabled:
        raise RuntimeError(
            'To run the logtransfer service it is required that '
            f'logstash formatting is enabled (found {config.logstash.enabled=}).'
        )

    is_logtransfer_enabled = config.logtransfer.enable_logtransfer
    is_statistics_logger_enabled = config.logtransfer.enable_statistics

    if not is_logtransfer_enabled and is_statistics_logger_enabled:
        raise ValueError(
            f'If {config.logtransfer.enable_statistics=} then the logstash '
            f'service must also be enabled (Got: {config.logtransfer.enable_logtransfer=})'
        )

    if config.logtransfer.enable_statistics:
        statistics_process = start_statistics_logger_process()
    else:
        statistics_process = None

    if config.logtransfer.enable_logtransfer:
        start_logtransfer_service()

    if config.logtransfer.enable_statistics:
        if statistics_process.is_alive():
            statistics_process.kill()
        statistics_process.join()


def run_app(
    with_gui: bool = False,
    gunicorn: bool = False,
    host: str = None,
    log_config: str = None,
    port: int = None,
    **kwargs,
):
    config.meta.service = 'app'

    if with_gui:
        import os
        import os.path
        import glob
        import shutil

        gui_folder = os.path.abspath(
            os.path.join(os.path.dirname(__file__), '../../app/static/gui')
        )
        run_gui_folder = os.path.join(
            config.fs.working_directory, 'run', 'gui_configured'
        )
        if not os.path.exists(run_gui_folder):
            os.makedirs(run_gui_folder)

        # copy
        shutil.rmtree(run_gui_folder, ignore_errors=True)
        shutil.copytree(gui_folder, run_gui_folder)

        # replace base path in all GUI files
        source_file_globs = [
            '**/*.json',
            '**/*.html',
            '**/*.js',
            '**/*.js.map',
            '**/*.css',
        ]
        for source_file_glob in source_file_globs:
            source_files = glob.glob(
                os.path.join(run_gui_folder, source_file_glob), recursive=True
            )
            for source_file in source_files:
                with open(source_file, 'rt') as f:
                    file_data = f.read()
                file_data = file_data.replace(
                    '/fairdi/nomad/latest', config.services.api_base_path
                )
                with open(source_file, 'wt') as f:
                    f.write(file_data)

        # App and gui are served from the same server, same port. Replace the base urls with
        # relative paths
        config.ui.app_base = f'{config.services.api_base_path.rstrip("/")}'
        config.ui.north_base = f'{config.services.api_base_path.rstrip("/")}/north'

    from nomad.utils import get_logger

    if gunicorn:
        from gunicorn.app.wsgiapp import WSGIApplication
        import logging.config

        if log_config:
            logging.config.fileConfig(log_config)

        if not kwargs.get('workers', None):
            kwargs['workers'] = 4

        class App(WSGIApplication):
            def __init__(self):
                self.app_uri = 'nomad.app.main:app'
                super().__init__()

            def load_config(self):
                self.cfg.set('timeout', config.services.api_timeout)
                self.cfg.set('worker_class', 'uvicorn.workers.UvicornWorker')
                if host or port:
                    self.cfg.set(
                        'bind',
                        f'{host if host else "0.0.0.0"}:{port if port else 8000}',
                    )
                for key, value in kwargs.items():
                    if key in self.cfg.settings and value is not None:
                        self.cfg.set(key, value)

        gunicorn_app = App()
        get_logger(__name__).info('created gunicorn server', data=str(gunicorn_app.cfg))
        gunicorn_app.run()
    else:
        from uvicorn import Server, Config

        kwargs['log_config'] = log_config

        uv_config = Config(
            'nomad.app.main:app',
            log_level='info',
            host=host,
            port=port if port else 8000,
            **{k: v for k, v in kwargs.items() if v is not None},
        )

        server = Server(config=uv_config)
        get_logger(__name__).info('created uvicorn server', data=uv_config.__dict__)
        server.run()


def run_worker():
    config.meta.service = 'worker'
    from nomad import processing

    processing.app.worker_main(['worker', '--loglevel=INFO', '-Q', 'celery'])


def run_hub():
    from jupyterhub.app import main
    import sys
    import os
    import subprocess

    if 'JUPYTERHUB_CRYPT_KEY' not in os.environ:
        crypt_key = config.north.jupyterhub_crypt_key
        if crypt_key is None:
            crypt_key = (
                subprocess.check_output('openssl rand -hex 32'.split(' '))
                .decode()
                .strip('\n')
            )
        os.environ['JUPYTERHUB_CRYPT_KEY'] = crypt_key

    config.meta.service = 'hub'
    sys.exit(
        main(argv=['-f', 'nomad/jupyterhub_config.py', '--Application.log_level=INFO'])
    )


def task_app(*args, **kwargs):
    logger = utils.get_logger('app')
    try:
        run_app(*args, **kwargs)
    except Exception as error:
        logger.exception(error)


def task_worker():
    logger = utils.get_logger('worker')
    try:
        run_worker()
    except Exception as error:
        logger.exception(error)


def appworker_(app_host=None, app_port=None):
    from concurrent import futures as concurrent_futures
    import asyncio

    app_kwargs = {'host': app_host, 'port': app_port}

    executor = concurrent_futures.ProcessPoolExecutor(2)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(executor, functools.partial(task_app, **app_kwargs))
    loop.run_in_executor(executor, task_worker)


@run.command(help='Run both app and worker.')
@click.option('--app-host', type=str, help='Passed as app host parameter.')
@click.option('--app-port', type=int, help='Passed as app port parameter.')
def appworker(app_host: str = None, app_port: int = None):
    appworker_(app_host, app_port)
