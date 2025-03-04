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
from __future__ import annotations

import copy
import dataclasses
import functools
import os
import re
from typing import Any, Callable, Type

import orjson
from fastapi import HTTPException
from mongoengine import Q

from nomad import utils
from nomad.app.v1.models import (
    MetadataPagination,
    Pagination,
    PaginationResponse,
    Metadata,
)
from nomad.app.v1.routers.datasets import DatasetPagination
from nomad.app.v1.routers.entries import perform_search
from nomad.app.v1.routers.uploads import (
    get_upload_with_read_access,
    upload_to_pydantic,
    entry_to_pydantic,
    UploadProcDataQuery,
    UploadProcDataPagination,
    EntryProcDataPagination,
)
from nomad.archive import ArchiveList, ArchiveDict, to_json
from nomad.graph.model import (
    RequestConfig,
    DefinitionType,
    DirectiveType,
    ResolveType,
    DatasetQuery,
    EntryQuery,
)
from nomad.datamodel import ServerContext, User, EntryArchive, Dataset
from nomad.datamodel.util import parse_path
from nomad.files import UploadFiles, RawPathInfo
from nomad.metainfo import (
    SubSection,
    QuantityReference,
    Reference,
    Quantity,
    SectionReference,
    JSON,
    Package,
    Definition,
    Section,
)
from nomad.metainfo.util import split_python_definition, MSubSectionList
from nomad.processing import Entry, Upload, ProcessStatus

logger = utils.get_logger(__name__)


@dataclasses.dataclass(frozen=True)
class Token:
    """
    Define the special tokens used to link one model/document/database to another.
    Ideally, these tokens should not collide with any existing keys.
    It is thus recommended to use a prefix to avoid collision.
    """

    DEF = 'm_def'
    RAW = 'files'
    ARCHIVE = 'archive'
    ENTRY = 'entry'
    ENTRIES = 'entries'
    UPLOAD = 'upload'
    UPLOADS = 'uploads'
    USER = 'user'
    USERS = 'users'
    DATASET = 'dataset'
    DATASETS = 'm_datasets'
    METAINFO = 'metainfo'
    SEARCH = 'search'
    ERROR = 'm_errors'
    METADATA = 'metadata'
    MAINFILE = 'mainfile'
    RESPONSE = 'm_response'


@dataclasses.dataclass(frozen=True)
class QueryError:
    NOACCESS = 'NOACCESS'
    NOTFOUND = 'NOTFOUND'
    ARCHIVEERROR = 'ARCHIVEERROR'
    GENERAL = 'GENERAL'


def dataset_to_pydantic(item):
    """
    Do NOT optimise this function.
    Function names are used to determine the type of the object.
    """
    return item.to_json()


class ArchiveError(Exception):
    """
    An exception raised when an error occurs in the archive.
    """

    pass


class ConfigError(Exception):
    """
    An exception raised when an error occurs in the configuration.
    """

    pass


@dataclasses.dataclass(frozen=True)
class GraphNode:
    upload_id: str  # the upload id of the current node
    entry_id: str  # the entry id of the current node
    current_path: list[str]  # the path in the result container
    result_root: dict  # the root of the result container
    ref_result_root: dict  # collection of referenced archives that should never change
    archive: Any  # current node in the archive
    archive_root: None | ArchiveDict | dict  # the root of the archive
    definition: Any  # definition of the current node
    visited_path: set[str]  # visited paths, for tracking circular references
    current_depth: int  # current depth, for tracking depth limit
    reader: Any  # the reader used to read the archive # pylint: disable=E0601

    @functools.cached_property
    def _prefix_to_remove(self):
        """Duplicated prefix to remove."""
        return f'uploads/{self.upload_id}/entries/{self.entry_id}/archive/'

    def replace(self, **kwargs):
        """
        Create a new `ArchiveNode` instance with the attributes of the current instance replaced.
        The `ArchiveNode` class is deliberately designed to be immutable.
        """
        return dataclasses.replace(self, **kwargs)

    def generate_reference(self, path: list = None) -> str:
        """
        Generate a reference string using a given path or the current path.
        """
        actual_path: list = path if path is not None else self.current_path
        actual_ref: str = '/'.join(str(v) for v in actual_path).removeprefix(
            self._prefix_to_remove
        )
        return f'{self._generate_prefix()}#/{actual_ref}'

    def _generate_prefix(self) -> str:
        return f'../uploads/{self.upload_id}/archive/{self.entry_id}'

    def goto(self, reference: str, resolve_inplace: bool) -> GraphNode:
        if reference.startswith(('/', '#')):
            return self._goto_local(reference, resolve_inplace)

        return self._goto_remote(reference, resolve_inplace)

    def _goto_local(self, reference: str, resolve_inplace: bool) -> GraphNode:
        """
        Go to a local reference.
        Since it is a local reference, only need to walk to the proper position.
        """
        reference_copy: str = reference
        # this is a local reference, go to the target location first
        while reference_copy.startswith(('/', '#')):
            reference_copy = reference_copy[1:]

        path_stack: list = [v for v in reference_copy.split('/') if v]

        if (reference_url := self.generate_reference(path_stack)) in self.visited_path:
            raise ArchiveError(f'Circular reference detected: {reference_url}.')

        try:
            target = self.__goto_path(self.archive_root, path_stack)
        except (KeyError, IndexError):
            raise ArchiveError(f'Archive {self.entry_id} does not contain {reference}.')

        return self._switch_root(
            self.replace(
                archive=target, visited_path=self.visited_path.union({reference_url})
            ),
            resolve_inplace,
            reference_url,
        )

    def _goto_remote(self, reference: str, resolve_inplace: bool) -> GraphNode:
        """
        Go to a remote archive, which can be either in the same server or another installation.
        """
        # this is a global reference, get the target archive first
        if (parse_result := parse_path(reference, self.upload_id)) is None:
            raise ArchiveError(f'Invalid reference: {reference}.')

        installation, other_upload_id, id_or_file, kind, path = parse_result

        if installation is not None:
            # todo: support cross installation reference
            raise ArchiveError(
                f'Cross installation reference is not supported yet: {reference}.'
            )

        if kind == 'raw':
            # it is a path to raw file
            # get the corresponding entry id
            other_entry: Entry = Entry.objects(
                upload_id=other_upload_id, mainfile=id_or_file
            ).first()
            if not other_entry:
                # cannot find the entry, None will be identified in the caller
                raise ArchiveError(
                    f'File {id_or_file} does not exist in upload {other_upload_id}.'
                )

            other_entry_id = other_entry.entry_id
        else:
            # it is an entry id
            other_entry_id = id_or_file

        path_stack: list = [v for v in path.split('/') if v]

        other_prefix: str = f'../uploads/{other_upload_id}/archive/{other_entry_id}'
        if (
            reference_url := f'{other_prefix}#/{"/".join(str(v) for v in path_stack)}'
        ) in self.visited_path:
            raise ArchiveError(f'Circular reference detected: {reference_url}.')

        # get the archive
        other_archive_root = self.reader.load_archive(other_upload_id, other_entry_id)

        try:
            # now go to the target path
            other_target = self.__goto_path(other_archive_root, path_stack)
        except (KeyError, IndexError):
            raise ArchiveError(f'Archive {other_entry_id} does not contain {path}.')

        return self._switch_root(
            self.replace(
                upload_id=other_upload_id,
                entry_id=other_entry_id,
                visited_path=self.visited_path.union({reference_url}),
                archive=other_target,
                archive_root=other_archive_root,
            ),
            resolve_inplace,
            reference_url,
        )

    def _switch_root(self, node: GraphNode, resolve_inplace: bool, reference_url: str):
        if resolve_inplace:
            # place the target into the current result container
            return node

        # place the reference into the current result container
        _populate_result(
            self.result_root,
            self.current_path,
            _convert_ref_to_path_string(reference_url),
        )

        return node.replace(
            current_path=_convert_ref_to_path(reference_url),
            result_root=self.ref_result_root,
        )

    @staticmethod
    def __goto_path(target_root: ArchiveDict | dict, path_stack: list) -> Any:
        """
        Go to the specified path in the data.
        """
        for key in path_stack:
            target_root = target_root[int(key) if key.isdigit() else key]
        return target_root


def _if_exists(target_root: dict, path_stack: list) -> bool:
    """
    Check if specified path in the data.
    """
    try:
        for key in path_stack:
            target_root = target_root[int(key) if key.isdigit() else key]
    except (KeyError, IndexError):
        return False
    return target_root is not None


@functools.lru_cache(maxsize=1024)
def _convert_ref_to_path(ref: str, upload_id: str = None) -> list:
    # test module name
    if '.' in (stripped_ref := ref.strip('.')):
        module_path, _ = split_python_definition(stripped_ref)
        return [Token.METAINFO, '.'.join(module_path[:-1])] + module_path[-1].split('/')

    # test reference
    parse_result = parse_path(ref, upload_id)
    if parse_result is None:
        raise ArchiveError(f'Invalid reference: {ref}.')

    installation, upload_id, entry_id, kind, path = parse_result
    if upload_id is None:
        raise ArchiveError(f'Invalid reference: {ref}, "upload_id" should be present.')

    path_stack: list = []

    if installation:
        path_stack.append(installation)

    path_stack.append(Token.UPLOADS)
    path_stack.append(upload_id)

    if kind == 'raw':
        path_stack.append(Token.RAW)
        path_stack.extend(v for v in entry_id.split('/') if v)
        path_stack.append(Token.ENTRIES)
    else:
        path_stack.append(Token.ENTRIES)
        path_stack.append(entry_id)

    path_stack.append(Token.ARCHIVE)
    path_stack.extend(v for v in path.split('/') if v)

    return path_stack


@functools.lru_cache(maxsize=1024)
def _convert_ref_to_path_string(ref: str, upload_id: str = None) -> str:
    return '/'.join(_convert_ref_to_path(ref, upload_id))


def _populate_result(container_root: dict, path: list, value, *, path_like=False):
    """
    For the given path and the root of the target container, populate the value.

    If `path_like` is set to `True`,
    the path will be treated as a true file system path such that
    numerical values are not interpreted as list indices.
    """

    def _merge_list(a: list, b: list):
        for i, v in enumerate(b):
            if i >= len(a):
                a.append(v)
            elif isinstance(a[i], dict) and isinstance(v, dict):
                _merge_dict(a[i], v)
            elif isinstance(a[i], list) and isinstance(v, list):
                _merge_list(a[i], v)
            elif a[i] is None:
                a[i] = v
            elif v is not None and a[i] != v:
                logger.warning(f'Cannot merge {a[i]} and {v}, potential conflicts.')

    def _merge_dict(a: dict, b: dict):
        for k, v in sorted(b.items(), key=lambda item: item[0]):
            if k not in a or a[k] is None:
                a[k] = v
            elif isinstance(a[k], set) and isinstance(v, set):
                a[k].update(v)
            elif isinstance(a[k], dict) and isinstance(v, dict):
                _merge_dict(a[k], v)
            elif isinstance(a[k], list) and isinstance(v, list):
                _merge_list(a[k], v)
            elif v is not None and a[k] != v:
                logger.warning(f'Cannot merge {a[k]} and {v}, potential conflicts.')

    def _set_default(
        container: dict | list, k_or_i: str | int, value_type: type
    ) -> dict | list:
        """
        Initialise empty containers at the given key or index.
        """
        if isinstance(container, dict):
            assert isinstance(k_or_i, str)
            container.setdefault(k_or_i, value_type())
            return container[k_or_i]

        assert isinstance(k_or_i, int)
        if container[k_or_i] is None:
            container[k_or_i] = value_type()
        return container[k_or_i]

    if len(path) == 0:
        assert isinstance(container_root, dict) and isinstance(value, dict)
        _merge_dict(container_root, value)
        return

    path_stack: list = list(reversed(path))
    target_container: dict | list = container_root
    key_or_index: None | str | int = None

    while len(path_stack) > 0:
        if key_or_index is not None:
            target_container = _set_default(target_container, key_or_index, dict)
        key_or_index = path_stack.pop()
        if path_like:
            continue
        while len(path_stack) > 0 and path_stack[-1].isdigit():
            target_container = _set_default(target_container, key_or_index, list)
            key_or_index = int(path_stack.pop())
            target_container.extend(  # type: ignore
                [None] * max(key_or_index - len(target_container) + 1, 0)
            )

    # the target container does not necessarily have to be a dict or a list
    # if the result is striped due to large size, it will be replaced by a string
    new_value = to_json(value)
    if isinstance(target_container, list):
        assert isinstance(key_or_index, int)
        if target_container[key_or_index] is None:
            target_container[key_or_index] = new_value
        elif isinstance(new_value, dict):
            _merge_dict(target_container[key_or_index], new_value)
        elif isinstance(new_value, list):
            _merge_list(target_container[key_or_index], new_value)
        else:
            target_container[key_or_index] = new_value
    elif isinstance(target_container, dict):
        assert isinstance(key_or_index, str)
        if isinstance(new_value, dict):
            target_container.setdefault(key_or_index, {})
            _merge_dict(target_container[key_or_index], new_value)
        elif isinstance(new_value, list):
            target_container.setdefault(key_or_index, [])
            _merge_list(target_container[key_or_index], new_value)
        else:
            target_container[key_or_index] = new_value


@functools.lru_cache(maxsize=1024)
def _parse_key(key: str | None) -> tuple:
    """
    Parse the name and index from the given key.
    The actual length of the target list is not known at this moment.
    The normalisation will be done when reading the actual data.
    """
    if key is None:
        return None, None

    # A[1]
    if matches := re.match(r'^([a-zA-z_\d]+)\[(-?\d+)]$', key):
        name = matches.group(1)
        start = int(matches.group(2))
        return name, (start,)

    # A[1:], A[:1], A[1:2]
    if matches := re.match(r'^([a-zA-z_\d]+)\[(-?\d+)?:(-?\d+)?]$', key):
        name = matches.group(1)
        start = int(matches.group(2)) if matches.group(2) else 0
        end = int(matches.group(3)) if matches.group(3) else None
        return name, (start, end)

    return key, None


def _normalise_required(
    required,
    config: RequestConfig,
    *,
    key: str = None,
    reader_type: Type[GeneralReader] = None,
):
    """
    Normalise the required dictionary.
    On exit, all leaves must be `RequestConfig` instances.
    """
    if isinstance(required, list):
        return [
            None
            if v is None
            else _normalise_required(v, config, key=key, reader_type=reader_type)
            for v in required
        ]

    name, index = _parse_key(key)

    # discard pagination and query so that they are not passed to the children
    config_dict: dict = {
        'property_name': name,
        'index': index,
        'pagination': None,
        'query': None,
    }

    can_query: bool = False

    if name in GeneralReader.__USER_ID__ or name == Token.USER or name == Token.USERS:
        reader_type = UserReader
    elif name in GeneralReader.__UPLOAD_ID__:
        reader_type = UploadReader
    elif name == Token.UPLOAD or name == Token.UPLOADS:
        reader_type = UploadReader
        can_query = True
    elif name in GeneralReader.__DATASET_ID__:
        reader_type = DatasetReader
    elif name == Token.DATASET or name == Token.DATASETS:
        reader_type = DatasetReader
        can_query = True
    elif name in GeneralReader.__ENTRY_ID__:
        reader_type = EntryReader
    elif name == Token.ENTRY or name == Token.ENTRIES:
        reader_type = EntryReader
        can_query = True
    elif name == Token.SEARCH:
        reader_type = ElasticSearchReader
        can_query = True
    elif name == Token.RAW or name == Token.MAINFILE:
        reader_type = FileSystemReader
    elif name == Token.ARCHIVE:
        reader_type = ArchiveReader

    _validate_config = functools.partial(
        reader_type.validate_config, key if key else 'top level'
    )

    # for backward compatibility
    if isinstance(required, str):
        if required in ('*', 'include'):
            return _validate_config(config.new(dict(config_dict, directive='plain')))
        if required == 'include-resolved':
            return _validate_config(config.new(dict(config_dict, directive='resolved')))

    if isinstance(required, dict):
        # check if there is a config dict
        new_config_dict = required.pop(GeneralReader.__CONFIG__, {})

        def _populate_query(_config_dict):
            if can_query:
                if new_query := required.pop('query', None):
                    _config_dict['query'] = new_query
                if new_pagination := required.pop('pagination', None):
                    _config_dict['pagination'] = new_pagination
                if GeneralReader.__WILDCARD__ in required:
                    _config_dict.setdefault(
                        'pagination', {'page': 1}
                    )  # a default pagination

        if isinstance(new_config_dict, dict):
            _populate_query(new_config_dict)
            combined: RequestConfig = config.new(dict(config_dict, **new_config_dict))
        elif isinstance(new_config_dict, str):
            child_config_dict: dict = {}
            if new_config_dict in ('*', 'include'):
                child_config_dict['directive'] = 'plain'
            elif new_config_dict == 'include-resolved':
                child_config_dict['directive'] = 'resolved'
            else:
                raise ConfigError(f'Invalid config: {new_config_dict}.')
            _populate_query(child_config_dict)
            combined = config.new(dict(config_dict, **child_config_dict))
        else:
            raise ConfigError(f'Invalid config: {new_config_dict}.')

        if len(required) == 0:
            # no more children, it is a leaf config
            return _validate_config(combined)

        # otherwise, it is a nested query
        subtree: dict = {
            k: _normalise_required(v, combined, key=k, reader_type=reader_type)
            for k, v in required.items()
        }
        if new_config_dict:
            # if there is a config dict, we need to add it to the subtree
            subtree[GeneralReader.__CONFIG__] = _validate_config(combined)
        return subtree

    # all other cases are not valid
    raise ConfigError(f'Invalid required config: {required}.')


def _parse_required(
    required_query: dict | str, reader_type: Type[GeneralReader]
) -> tuple[dict | RequestConfig, RequestConfig]:
    # extract global config if present
    # do not modify the original dict as the same dict may be used elsewhere
    if isinstance(required_query, dict):
        required_copy = copy.deepcopy(required_query)

        global_dict: dict = {}

        # for backward compatibility
        resolve_inplace: bool | None = None
        for key in ('resolve_inplace', 'resolve-inplace'):
            if resolve_inplace is None:
                resolve_inplace = required_copy.pop(key, None)
        if isinstance(resolve_inplace, bool):
            global_dict['resolve_inplace'] = resolve_inplace

        # normalise the query by replacing '-' with '_'
        global_config = RequestConfig.parse_obj(global_dict)
        # extract query config for each field
        return _normalise_required(
            required_copy, global_config, reader_type=reader_type
        ), global_config

    if required_query in ('*', 'include'):
        # for backward compatibility
        global_config = RequestConfig.parse_obj({'directive': 'plain'})
        return global_config, global_config

    if required_query == 'include-resolved':
        # for backward compatibility
        global_config = RequestConfig.parse_obj({'directive': 'resolved'})
        return global_config, global_config

    raise ConfigError(f'Invalid required config: {required_query}.')


@functools.lru_cache(maxsize=1024)
def _normalise_index(index: tuple | None, length: int) -> range:
    """
    Normalise the index to a [-length,length).
    """
    # all items
    if index is None:
        return range(length)

    def _bound(v):
        if v < -length:
            return -length
        if v >= length:
            return length - 1
        return v

    # one item
    if len(index) == 1:
        start = _bound(index[0])
        return range(start, start + 1)

    # a range of items
    start, end = index
    if start is None:
        start = 0
    if end is None:
        end = length

    return range(_bound(start), _bound(end) + 1)


class GeneralReader:
    # controls the name of configuration
    # it will be extracted from the query dict to generate the configuration object
    __CONFIG__: str = 'm_request'
    # controls the wildcard identifier
    # wildcard is used in mongo data (upload, entry, dataset) to apply a different configuration
    # to records other than the explicitly specified ones
    __WILDCARD__: str = '*'
    # controls the names of fields that are treated as user id, for those fields,
    # implicit resolve is supported and explicit resolve does not require an explicit resolve type
    __USER_ID__: set = {
        'main_author',
        'coauthors',
        'reviewers',
        'viewers',
        'writers',
        'entry_coauthors',
        'user_id',
    }
    # controls the names of fields that are treated as entry id, for those fields,
    # implicit resolve is supported and explicit resolve does not require an explicit resolve type
    __ENTRY_ID__: set = {'entry_id'}
    # controls the names of fields that are treated as upload id, for those fields,
    # implicit resolve is supported and explicit resolve does not require an explicit resolve type
    __UPLOAD_ID__: set = {'upload_id'}
    # controls the names of fields that are treated as dataset id
    __DATASET_ID__: set = {'datasets'}
    __CACHE__: str = '__CACHE__'

    def __init__(
        self,
        required_query: dict | str | RequestConfig,
        *,
        user=None,
        init: bool = True,
        config: RequestConfig = None,
        global_root: dict = None,
    ):
        """
        Supports two modes of initialisation:
        1. Provide `required_query` and `user` only.
            This is the mode that should be used by the external.
            The reader will be initialised with `required_query` and `user`.
            The `required_query` is a dict and will be validated.
            The corresponding configuration dicts will be converted to `RequestConfig` objects.
        2. Provide `required_query`, `user`, `init=False`, `config` and optionally, `global_root`.
            This is the mode used by the internal to switch between different readers.
            The `init` flag must be set to `False` to indicate that the reader should not initialise itself.
            The `required_query` is validated and converted, can be either a `dict` or a `RequestConfig`.
            The `config` is a `RequestConfig` object that holds the parent configuration.
            It is necessary as not all subtrees have a configuration.
            The parent configuration needs to be passed down to the children.
            The `global_root` is used in child readers to allow them to populate data to global root.
            This helps to reduce the nesting level of the final response dict.
        """

        # maybe used to retrieve additional information
        self.user: User = user
        # use to provide a link to the final response dict
        # so that some data can be populated in different places
        self.global_root: dict = global_root

        # for cacheing
        # can only store uploads in the reader
        # due to limitations of upload class (open/close limitations)
        self.upload_pool: dict[str, UploadFiles] = {}

        self.required_query: dict | RequestConfig
        if not init:
            assert config is not None
            self.global_config: RequestConfig = config
            assert not isinstance(required_query, str)
            self.required_query = required_query
        else:
            assert not isinstance(required_query, RequestConfig)
            self.required_query, self.global_config = _parse_required(
                required_query, self.__class__
            )

        self.errors: dict[str, set] = {}

    def _populate_error_list(self, container: dict):
        if not self.errors:
            return

        error_list = container.setdefault(Token.ERROR, [])
        for error_type, error_set in self.errors.items():
            error_list.extend(
                {'error_type': error_type, 'message': error_item}
                for error_item in error_set
            )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        for upload in self.upload_pool.values():
            upload.close()

    def _log(
        self,
        message: str,
        *,
        error_type: str = QueryError.GENERAL,
        to_response: bool = True,
    ):
        logger.debug(message)
        if to_response:
            self.errors.setdefault(error_type, set()).add(message)

    def _check_cache(self, path: str | list, config_hash=None) -> bool:
        """
        Check if the given path has been cached.
        Optionally, using the config hash to identify different configurations.
        """
        if isinstance(path, list):
            path = '/'.join(path)

        cache_pool = self.global_root.setdefault(GeneralReader.__CACHE__, {})

        return (
            config_hash is None
            and path in cache_pool
            or config_hash in cache_pool.get(path, set())
        )

    def _cache_hash(self, path: str | list, config_hash=None):
        """
        Check if the given path has been cached.
        Optionally, using the config hash to identify different configurations.
        """
        if isinstance(path, list):
            path = '/'.join(path)

        self.global_root.setdefault(GeneralReader.__CACHE__, {}).setdefault(
            path, set()
        ).add(config_hash)

    def retrieve_user(self, user_id: str) -> str | dict:
        # `me` is a convenient way to refer to the current user
        if user_id == 'me':
            user_id = self.user.user_id

        try:
            user: User = User.get(user_id=user_id)
        except Exception as e:
            self._log(str(e), to_response=False)
            return user_id

        if user is None:
            self._log(
                f'The value {user_id} is not a valid user id.',
                error_type=QueryError.NOTFOUND,
            )
            return user_id

        return user.m_to_dict(with_out_meta=True, include_derived=True)

    def _overwrite_upload(self, item: Upload):
        plain_dict = orjson.loads(upload_to_pydantic(item).json())
        if n_entries := plain_dict.pop('entries', None):
            plain_dict['n_entries'] = n_entries
        plain_dict['processing_successful'] = item.processed_entries_count
        plain_dict['processing_failed'] = (
            item.total_entries_count - item.processed_entries_count
        )

        if main_author := plain_dict.pop('main_author', None):
            plain_dict['main_author'] = self.retrieve_user(main_author)
        for name in ('coauthors', 'reviewers', 'viewers', 'writers'):
            if (items := plain_dict.pop(name, None)) is not None:
                plain_dict[name] = [self.retrieve_user(item) for item in items]

        return plain_dict

    def retrieve_upload(self, upload_id: str) -> str | dict:
        try:
            upload: Upload = get_upload_with_read_access(
                upload_id, self.user, include_others=True
            )
        except HTTPException as e:
            if e.status_code == 404:
                self._log(
                    f'The value {upload_id} is not a valid upload id.',
                    error_type=QueryError.NOTFOUND,
                )
            else:
                self._log(
                    f'No access to upload {upload_id}.', error_type=QueryError.NOACCESS
                )
            return upload_id

        return self._overwrite_upload(upload)

    @staticmethod
    def _overwrite_entry(item: Entry):
        plain_dict = orjson.loads(entry_to_pydantic(item).json())
        plain_dict.pop('entry_metadata', None)
        if mainfile := plain_dict.pop('mainfile', None):
            plain_dict['mainfile_path'] = mainfile
        if datasets := plain_dict.pop('datasets', None):
            plain_dict['dataset_ids'] = datasets

        return plain_dict

    def retrieve_entry(self, entry_id: str) -> str | dict:
        if (
            perform_search(
                owner='all', query={'entry_id': entry_id}, user_id=self.user.user_id
            ).pagination.total
            == 0
        ):
            self._log(
                f'The value {entry_id} is not a valid entry id or not visible to current user.',
                error_type=QueryError.NOACCESS,
            )
            return entry_id

        return self._overwrite_entry(Entry.objects(entry_id=entry_id).first())

    def retrieve_dataset(self, dataset_id: str) -> str | dict:
        if (
            dataset := Dataset.m_def.a_mongo.objects(dataset_id=dataset_id).first()
        ) is None:
            self._log(
                f'The value {dataset_id} is not a valid dataset id.',
                error_type=QueryError.NOTFOUND,
            )
            return dataset_id

        if dataset.user_id != self.user.user_id:
            self._log(
                f'No access to dataset {dataset_id}.', error_type=QueryError.NOACCESS
            )
            return dataset_id

        return dataset.to_mongo().to_dict()

    def load_archive(self, upload_id: str, entry_id: str) -> ArchiveDict:
        if upload_id not in self.upload_pool:
            # get the archive
            # does the current user have access to the target archive?
            try:
                upload: Upload = get_upload_with_read_access(
                    upload_id, self.user, include_others=True
                )
            except HTTPException:
                raise ArchiveError(
                    f'Current user does not have access to upload {upload_id}.'
                )

            if upload.upload_files is None:
                raise ArchiveError(f'Upload {upload_id} does not exist.')

            self.upload_pool[upload_id] = upload.upload_files

        try:
            return self.upload_pool[upload_id].read_archive(entry_id)[entry_id]
        except KeyError:
            raise ArchiveError(
                f'Archive {entry_id} does not exist in upload {entry_id}.'
            )

    def _apply_resolver(self, node: GraphNode, config: RequestConfig):
        if_skip: bool = config.property_name not in GeneralReader.__UPLOAD_ID__
        if_skip &= config.property_name not in GeneralReader.__USER_ID__
        if_skip &= config.property_name not in GeneralReader.__DATASET_ID__
        if_skip &= config.property_name not in GeneralReader.__ENTRY_ID__
        if_skip &= config.resolve_type is None
        if_skip |= config.directive is DirectiveType.plain
        if if_skip:
            return node.archive

        if not isinstance(node.archive, str):
            self._log(f'The value {node.archive} is not a valid id.', to_response=False)
            return node.archive

        if config.resolve_type is ResolveType.user:
            return self.retrieve_user(node.archive)
        if config.resolve_type is ResolveType.upload:
            return self.retrieve_upload(node.archive)
        if config.resolve_type is ResolveType.entry:
            return self.retrieve_entry(node.archive)
        if config.resolve_type is ResolveType.dataset:
            return self.retrieve_dataset(node.archive)

        return node.archive

    def _resolve_list(self, node: GraphNode, config: RequestConfig):
        # the original archive may be an empty list
        # populate an empty list to keep the structure
        _populate_result(node.result_root, node.current_path, [])
        new_config: RequestConfig = config.new({'index': None}, retain_pattern=True)
        for i in _normalise_index(config.index, len(node.archive)):
            self._resolve(
                node.replace(
                    archive=node.archive[i], current_path=node.current_path + [str(i)]
                ),
                new_config,
            )

    def _walk(
        self,
        node: GraphNode,
        required: dict | RequestConfig,
        parent_config: RequestConfig,
    ):
        raise NotImplementedError()

    def _resolve(
        self,
        node: GraphNode,
        config: RequestConfig,
        *,
        omit_keys=None,
        wildcard: bool = False,
    ):
        raise NotImplementedError()

    @classmethod
    def validate_config(cls, key: str, config: RequestConfig):
        raise NotImplementedError()


class MongoReader(GeneralReader):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entries = None
        self.uploads = None
        self.datasets = None

    def _query_es(self, config: RequestConfig):
        search_params: dict = {
            'owner': 'user',
            'user_id': self.user.user_id,
            'query': {},
            # 'required': MetadataRequired(include=['entry_id'])
        }
        search_query: dict = {}

        if config.query:
            assert isinstance(config.query, Metadata)

            search_query = config.query.dict(exclude_none=True)  # type: ignore

            if config.query.owner:
                search_params['owner'] = config.query.owner
            if config.query.query:
                search_params['query'] = config.query.query
            if config.query.pagination:
                search_params['pagination'] = config.query.pagination
            if config.query.required:
                search_params['required'] = config.query.required
            if config.query.aggregations:
                search_params['aggregations'] = config.query.aggregations

        if config.pagination and (not config.query or not config.query.pagination):  # type: ignore
            search_params['pagination'] = config.pagination

        search_response = perform_search(**search_params)
        # overwrite the pagination to the new one from the search response
        config.pagination = search_response.pagination

        def _overwrite(item):
            if mainfile := item.pop('mainfile', None):
                item['mainfile_path'] = mainfile
            return item

        return search_query, {
            v['entry_id']: _overwrite(v) for v in search_response.data
        }

    def _query_entries(self, config: RequestConfig):
        if not config.query:
            return None, self.entries

        assert isinstance(config.query, EntryQuery)

        mongo_query = Q()
        if config.query.parser_name:
            parser_name = Q()
            for item in config.query.parser_name:
                if item:
                    parser_name |= Q(parser_name__regex=item)
            mongo_query &= parser_name

        if config.query.mainfile:
            mainfile = Q()
            for item in config.query.mainfile:
                if item:
                    mainfile |= Q(mainfile__regex=item)
            mongo_query &= mainfile

        if config.query.references:
            references = Q()
            for item in config.query.references:
                if item:
                    references |= Q(references__regex=item)
            mongo_query &= references

        return config.query.dict(exclude_unset=True), self.uploads.filter(mongo_query)

    def _query_uploads(self, config: RequestConfig):
        if not config.query:
            return None, self.uploads

        assert isinstance(config.query, UploadProcDataQuery)

        mongo_query = Q()
        if config.query.upload_id:
            mongo_query &= Q(upload_id__in=config.query.upload_id)
        if config.query.upload_name:
            mongo_query &= Q(upload_name__in=config.query.upload_name)
        if config.query.process_status is not None:
            mongo_query &= Q(process_status=config.query.process_status)
        elif config.query.is_processing is True:
            mongo_query &= Q(process_status__in=ProcessStatus.STATUSES_PROCESSING)
        elif config.query.is_processing is False:
            mongo_query &= Q(process_status__in=ProcessStatus.STATUSES_NOT_PROCESSING)
        if config.query.is_published is True:
            mongo_query &= Q(publish_time__ne=None)
        elif config.query.is_published is False:
            mongo_query &= Q(publish_time=None)
        if config.query.is_owned is True:
            mongo_query &= Q(main_author=self.user.user_id)
        elif config.query.is_owned is False:
            mongo_query &= Q(main_author__ne=self.user.user_id)

        return config.query.dict(exclude_unset=True), self.uploads.filter(mongo_query)

    def _query_datasets(self, config: RequestConfig):
        if not config.query:
            return None, self.datasets

        assert isinstance(config.query, DatasetQuery)

        mongo_query = Q()
        if config.query.dataset_id:
            mongo_query &= Q(dataset_id=config.query.dataset_id)
        if config.query.dataset_name:
            mongo_query &= Q(dataset_name=config.query.dataset_name)
        if config.query.user_id:
            mongo_query &= Q(user_id__in=config.query.user_id)
        if config.query.dataset_type:
            mongo_query &= Q(dataset_type=config.query.dataset_type)
        if config.query.doi:
            mongo_query &= Q(doi=config.query.doi)
        if config.query.prefix:
            mongo_query &= Q(
                dataset_name=re.compile(rf'^{config.query.prefix}.*$', re.IGNORECASE)
            )

        return config.query.dict(exclude_unset=True), self.datasets.filter(mongo_query)

    def _normalise(
        self, mongo_result, config: RequestConfig, transformer: Callable
    ) -> tuple[dict, PaginationResponse | None]:
        """
        Apply pagination and transform to the mongo search results.
        """
        pagination_response: PaginationResponse | None = None
        # PaginationResponse comes from elastic search that has been processed
        # later we skip applying pagination
        if isinstance(config.pagination, PaginationResponse):
            pagination_response = config.pagination
        elif isinstance(config.pagination, Pagination):
            pagination_response = PaginationResponse(
                total=mongo_result.count() if mongo_result else 0,
                **config.pagination.dict(),
            )

        if transformer is None:
            return mongo_result, pagination_response

        if mongo_result is None:
            return {}, pagination_response

        # apply pagination when config.pagination is present
        # if it is a PaginationResponse, it means the pagination has been applied in the search
        if config.pagination is not None and not isinstance(
            config.pagination, PaginationResponse
        ):
            assert isinstance(config.pagination, Pagination)

            mongo_result = config.pagination.order_result(mongo_result)

            def _pick_id(_item):
                if transformer == upload_to_pydantic:
                    return _item.upload_id
                if transformer == dataset_to_pydantic:
                    return _item.dataset_id
                if transformer == entry_to_pydantic:
                    return _item.entry_id

                raise ValueError(f'Should not reach here.')

            mongo_result = config.pagination.paginate_result(mongo_result, _pick_id)

            if mongo_result:
                pagination_response.next_page_after_value = _pick_id(
                    mongo_result[len(mongo_result) - 1]
                )

        if transformer == upload_to_pydantic:
            mongo_dict = {
                v['upload_id']: v
                for v in [self._overwrite_upload(item) for item in mongo_result]
            }
        elif transformer == dataset_to_pydantic:
            mongo_dict = {
                v['dataset_id']: v
                for v in [orjson.loads(transformer(item)) for item in mongo_result]
            }
        elif transformer == entry_to_pydantic:
            mongo_dict = {
                v['entry_id']: v
                for v in [self._overwrite_entry(item) for item in mongo_result]
            }
        else:
            raise ValueError(f'Should not reach here.')

        return mongo_dict, pagination_response

    def read(self):
        """
        All read() methods, including the ones in the subclasses, should return a dict as the response.
        It performs the following tasks:
            1. Define the default maximum scope the current user can reach by defining any of `self.uploads`,
                `self.entries` and `self.datasets`.
            2. Define the current `ArchiveNode` object.
            3. Call `_walk()` to walk through the request tree.

        In this method, as a general reader, it populates all three: `self.uploads`, `self.entries` and `self.datasets`.
        In other methods, it may only populate one or two of them, which represents the available edges to the current
        node.
        For example, in a `UploadReader`, it only populates `self.entries`, which implies that from an upload, one can
        only navigate to its entries, using `Token.ENTRIES` token.
        """
        response: dict = {}

        self.global_root = response

        self.uploads = Upload.objects(
            Q(main_author=self.user.user_id)
            | Q(reviewers=self.user.user_id)
            | Q(coauthors=self.user.user_id)
        )
        self.entries = Entry.objects(upload_id__in=[v.upload_id for v in self.uploads])
        self.datasets = Dataset.m_def.a_mongo.objects(user_id=self.user.user_id)

        self._walk(
            GraphNode(
                upload_id='__NOT_NEEDED__',
                entry_id='__NOT_NEEDED__',
                current_path=[],
                result_root=response,
                ref_result_root=self.global_root,
                archive=None,
                archive_root=None,
                definition=None,
                visited_path=set(),
                current_depth=0,
                reader=self,
            ),
            self.required_query,
            self.global_config,
        )

        self._populate_error_list(response)

        return response

    def _walk(
        self,
        node: GraphNode,
        required: dict | RequestConfig,
        parent_config: RequestConfig,
    ):
        if isinstance(required, RequestConfig):
            return self._resolve(node, required)

        has_config: bool = GeneralReader.__CONFIG__ in required
        has_wildcard: bool = GeneralReader.__WILDCARD__ in required

        current_config: RequestConfig = required.get(
            GeneralReader.__CONFIG__, parent_config
        )

        if has_wildcard:
            wildcard_config = required[GeneralReader.__WILDCARD__]
            if isinstance(wildcard_config, RequestConfig):
                # use the wildcard config to filter the current scope
                self._resolve(
                    node, wildcard_config, omit_keys=required.keys(), wildcard=True
                )
            elif isinstance(node.archive, dict):
                # nested fuzzy query, add all available keys to the required
                # !!!
                # DO NOT directly use .update() as it will modify the original dict
                # !!!
                extra_required = {
                    k: wildcard_config for k in node.archive.keys() if k not in required
                }
                required = required | extra_required
        elif has_config:
            # use the inherited/assigned config to filter the current scope
            self._resolve(node, current_config, omit_keys=required.keys())

        offload_pack: dict = {
            'user': self.user,
            'init': False,
            'config': current_config,
            'global_root': self.global_root,
        }

        for key, value in required.items():
            if key in (GeneralReader.__CONFIG__, GeneralReader.__WILDCARD__):
                continue

            def offload_read(reader_cls, *args, read_list=False):
                try:
                    with reader_cls(value, **offload_pack) as reader:
                        _populate_result(
                            node.result_root,
                            node.current_path + [key],
                            [reader.read(item) for item in args[0]]
                            if read_list
                            else reader.read(*args),
                        )
                except Exception as exc:
                    self._log(str(exc))

            if key == Token.RAW and self.__class__ is UploadReader:
                # hitting the bottom of the current scope
                offload_read(FileSystemReader, node.upload_id)
                continue

            if key == Token.METADATA and self.__class__ is EntryReader:
                # hitting the bottom of the current scope
                offload_read(ElasticSearchReader, node.entry_id)
                continue

            if key == Token.UPLOAD and self.__class__ is EntryReader:
                # hitting the bottom of the current scope
                offload_read(UploadReader, node.upload_id)
                continue

            if key == Token.MAINFILE and self.__class__ is EntryReader:
                # hitting the bottom of the current scope
                offload_read(
                    FileSystemReader, node.upload_id, node.archive['mainfile_path']
                )
                continue

            if key == Token.ARCHIVE and self.__class__ is EntryReader:
                # hitting the bottom of the current scope
                offload_read(ArchiveReader, node.upload_id, node.entry_id)
                continue

            if key == Token.ENTRIES and self.__class__ is ElasticSearchReader:
                # hitting the bottom of the current scope
                offload_read(EntryReader, node.archive['entry_id'])
                continue

            if isinstance(node.archive, dict) and isinstance(value, dict):
                # treat it as a normal key
                # and handle in applying resolver if it is a leaf node
                if key in GeneralReader.__ENTRY_ID__:
                    # offload to the upload reader if it is a nested query
                    if isinstance(entry_id := node.archive.get(key, None), str):
                        offload_read(EntryReader, entry_id)
                        continue

                if key in GeneralReader.__UPLOAD_ID__:
                    # offload to the entry reader if it is a nested query
                    if isinstance(upload_id := node.archive.get(key, None), str):
                        offload_read(UploadReader, upload_id)
                        continue

                if key in GeneralReader.__USER_ID__:
                    # offload to the user reader if it is a nested query
                    if user_id := node.archive.get(key, None):
                        offload_read(
                            UserReader, user_id, read_list=isinstance(user_id, list)
                        )
                        continue

            if isinstance(value, RequestConfig):
                child_config = value
            elif isinstance(value, dict) and GeneralReader.__CONFIG__ in value:
                child_config = value[GeneralReader.__CONFIG__]
            else:
                child_config = current_config.new({'query': None, 'pagination': None})

            def __offload_walk(query_set, transformer):
                response_path: list = node.current_path + [key, Token.RESPONSE]

                query, filtered = query_set
                if query is not None:
                    _populate_result(node.result_root, response_path + ['query'], query)

                if filtered is None:
                    return

                result, pagination = self._normalise(
                    filtered, child_config, transformer
                )
                if pagination is not None:
                    _populate_result(
                        node.result_root,
                        response_path + ['pagination'],
                        pagination.dict(),
                    )
                self._walk(
                    node.replace(
                        archive={k: k for k in result}
                        if isinstance(value, RequestConfig)
                        else result,
                        current_path=node.current_path + [key],
                    ),
                    value,
                    current_config,
                )

            if self._offload_walk(__offload_walk, child_config, key, value):
                continue

            if len(node.current_path) > 0 and node.current_path[-1] in __M_SEARCHABLE__:
                offload_read(__M_SEARCHABLE__[node.current_path[-1]], key)
                continue

            # key may contain index, cached
            name, index = _parse_key(key)

            if name not in node.archive:
                continue

            child_archive = node.archive[name]
            child_path: list = node.current_path + [name]

            if isinstance(value, RequestConfig):
                self._resolve(
                    node.replace(archive=child_archive, current_path=child_path), value
                )
            elif isinstance(value, dict):
                # should never reach here in most cases
                # most mongo data is a 1-level tree
                # second level implies it's delegated to another reader
                def __walk(__archive, __path):
                    self._walk(
                        node.replace(archive=__archive, current_path=__path),
                        value,
                        current_config,
                    )

                if isinstance(child_archive, list):
                    for i in _normalise_index(index, len(child_archive)):
                        __walk(child_archive[i], child_path + [str(i)])
                else:
                    __walk(child_archive, child_path)
            elif isinstance(value, list):
                # optionally support alternative syntax
                pass
            else:
                # should never reach here
                raise ConfigError(f'Invalid required config: {value}.')

    def _offload_walk(
        self, offload_func: Callable, config: RequestConfig, key: str, value
    ) -> bool:
        if key == Token.SEARCH:
            offload_func(self._query_es(config), None)
            return True
        if key == Token.ENTRY or key == Token.ENTRIES:
            offload_func(self._query_entries(config), entry_to_pydantic)
            return True
        if key == Token.UPLOAD or key == Token.UPLOADS:
            offload_func(self._query_uploads(config), upload_to_pydantic)
            return True
        if key == Token.DATASET or key == Token.DATASETS:
            offload_func(self._query_datasets(config), dataset_to_pydantic)
            return True
        if key == Token.USER or key == Token.USERS:
            offload_func(
                (
                    None,
                    {
                        k: v
                        for k, v in value.items()
                        if k
                        not in (GeneralReader.__CONFIG__, GeneralReader.__WILDCARD__)
                    },
                ),
                None,
            )
            return True

        return False

    def _resolve(
        self,
        node: GraphNode,
        config: RequestConfig,
        *,
        omit_keys=None,
        wildcard: bool = False,
    ):
        if isinstance(node.archive, list):
            return self._resolve_list(node, config)

        if not isinstance(node.archive, dict):
            # primitive type data is always included
            # this is not affected by size limit nor by depth limit
            return _populate_result(
                node.result_root, node.current_path, self._apply_resolver(node, config)
            )

        if (
            config.directive is DirectiveType.resolved
            and len(node.current_path) > 1
            and node.current_path[-2] in __M_SEARCHABLE__
        ):
            offload_reader = __M_SEARCHABLE__[node.current_path[-2]]
            try:
                with offload_reader(
                    config,
                    user=self.user,
                    init=False,
                    config=config,
                    global_root=self.global_root,
                ) as reader:
                    _populate_result(
                        node.result_root,
                        node.current_path,
                        reader.read(node.current_path[-1]),
                    )
            except Exception as e:
                self._log(str(e))
            return

        if wildcard:
            assert omit_keys is not None

        for key in sorted(node.archive.keys()):
            new_config: dict = {'property_name': key, 'index': None}
            if wildcard:
                if any(k.startswith(key) for k in omit_keys):
                    continue
            else:
                if (
                    not config.if_include(key)
                    or omit_keys is not None
                    and any(k.startswith(key) for k in omit_keys)
                ):
                    continue
                new_config['include'] = ['*']
                new_config['exclude'] = None

            # need to retrain the include/exclude pattern for wildcard
            self._resolve(
                node.replace(
                    archive=node.archive[key],
                    current_path=node.current_path + [key],
                    current_depth=node.current_depth + 1,
                ),
                config.new(new_config, retain_pattern=wildcard),
            )

    @classmethod
    def validate_config(cls, key: str, config: RequestConfig):
        if config.include_definition is not DefinitionType.none:
            raise ConfigError(
                f'Including definitions is not supported in {cls.__name__} @ {key}.'
            )
        if config.depth:
            raise ConfigError(
                f'Limiting result depth is not supported in {cls.__name__} @ {key}.'
            )
        if config.resolve_depth:
            raise ConfigError(
                f'Limiting resolve depth is not supported in {cls.__name__} @ {key}.'
            )
        if config.max_list_size or config.max_dict_size:
            raise ConfigError(
                f'Limiting container size is not supported in {cls.__name__} @ {key}.'
            )

        if (
            (config.pagination or config.query)
            and key not in __M_SEARCHABLE__
            and key != 'top level'
        ):
            raise ConfigError(
                f'Pagination and query are only supported for searchable keys '
                f'{__M_SEARCHABLE__.keys()} in {cls.__name__}. Revise config @ {key}.'
            )

        return config


class UploadReader(MongoReader):
    # noinspection PyMethodOverriding
    def read(self, upload_id: str) -> dict:  # type: ignore
        response: dict = {}

        if self.global_root is None:
            self.global_root = response
            has_global_root: bool = False
        else:
            has_global_root = True

        # if it is a string, no access
        if isinstance(target_upload := self.retrieve_upload(upload_id), dict):
            self.entries = Entry.objects(upload_id=upload_id)

            self._walk(
                GraphNode(
                    upload_id=upload_id,
                    entry_id='__NOT_NEEDED__',
                    current_path=[],
                    result_root=response,
                    ref_result_root=self.global_root,
                    archive=target_upload,
                    archive_root=None,
                    definition=None,
                    visited_path=set(),
                    current_depth=0,
                    reader=self,
                ),
                self.required_query,
                self.global_config,
            )

        self._populate_error_list(response)

        # if there is a global root, it is a sub-query, no need to clear it
        # if there is no global root, it is a top-level query, clear it
        # mainly to make the reader reentrant, although one reader should not be used multiple times
        if not has_global_root:
            self.global_root = None

        return response

    @classmethod
    def validate_config(cls, key: str, config: RequestConfig):
        try:
            if config.query is not None:
                config.query = UploadProcDataQuery.parse_obj(config.query)
            if config.pagination is not None:
                config.pagination = UploadProcDataPagination.parse_obj(
                    config.pagination
                )
        except Exception as e:
            raise ConfigError(str(e))

        return super().validate_config(key, config)


class DatasetReader(MongoReader):
    # noinspection PyMethodOverriding
    def read(self, dataset_id: str) -> dict:  # type: ignore
        response: dict = {}

        if self.global_root is None:
            self.global_root = response
            has_global_root: bool = False
        else:
            has_global_root = True

        # if it is a string, no access
        if isinstance(target_dataset := self.retrieve_dataset(dataset_id), dict):
            self.entries = Entry.objects(datasets=dataset_id)
            self.uploads = Upload.objects(
                upload_id__in=list({v['upload_id'] for v in self.entries})
            )

            self._walk(
                GraphNode(
                    upload_id='__NOT_NEEDED__',
                    entry_id='__NOT_NEEDED__',
                    current_path=[],
                    result_root=response,
                    ref_result_root=self.global_root,
                    archive=target_dataset,
                    archive_root=None,
                    definition=None,
                    visited_path=set(),
                    current_depth=0,
                    reader=self,
                ),
                self.required_query,
                self.global_config,
            )

        self._populate_error_list(response)

        if not has_global_root:
            self.global_root = None

        return response

    @classmethod
    def validate_config(cls, key: str, config: RequestConfig):
        try:
            if config.query is not None:
                config.query = DatasetQuery.parse_obj(config.query)
            if config.pagination is not None:
                config.pagination = DatasetPagination.parse_obj(config.pagination)
        except Exception as e:
            raise ConfigError(str(e))

        return super().validate_config(key, config)


class EntryReader(MongoReader):
    # noinspection PyMethodOverriding
    def read(self, entry_id: str) -> dict:  # type: ignore
        response: dict = {}

        if self.global_root is None:
            self.global_root = response
            has_global_root: bool = False
        else:
            has_global_root = True

        # if it is a string, no access
        if isinstance(target_entry := self.retrieve_entry(entry_id), dict):
            self.datasets = Dataset.m_def.a_mongo.objects(entries=entry_id)

            self._walk(
                GraphNode(
                    upload_id=target_entry['upload_id'],
                    entry_id=entry_id,
                    current_path=[],
                    result_root=response,
                    ref_result_root=self.global_root,
                    archive=target_entry,
                    archive_root=None,
                    definition=None,
                    visited_path=set(),
                    current_depth=0,
                    reader=self,
                ),
                self.required_query,
                self.global_config,
            )

        self._populate_error_list(response)

        if not has_global_root:
            self.global_root = None

        return response

    @classmethod
    def validate_config(cls, key: str, config: RequestConfig):
        try:
            if config.query is not None:
                config.query = EntryQuery.parse_obj(config.query)
            if config.pagination is not None:
                config.pagination = EntryProcDataPagination.parse_obj(config.pagination)
        except Exception as e:
            raise ConfigError(str(e))

        return super().validate_config(key, config)


class ElasticSearchReader(EntryReader):
    def retrieve_entry(self, entry_id: str) -> str | dict:
        search_response = perform_search(
            owner='all', query={'entry_id': entry_id}, user_id=self.user.user_id
        )

        if search_response.pagination.total == 0:
            self._log(
                f'The value {entry_id} is not a valid entry id or not visible to current user.',
                error_type=QueryError.NOACCESS,
            )
            return entry_id

        plain_dict = search_response.data[0]
        if mainfile := plain_dict.pop('mainfile', None):
            plain_dict['mainfile_path'] = mainfile

        return plain_dict

    @classmethod
    def validate_config(cls, key: str, config: RequestConfig):
        try:
            if config.query is not None:
                config.query = Metadata.parse_obj(config.query)
            if config.pagination is not None:
                config.pagination = MetadataPagination.parse_obj(config.pagination)
        except Exception as e:
            raise ConfigError(str(e))

        return MongoReader.validate_config(key, config)


class UserReader(MongoReader):
    # noinspection PyMethodOverriding
    def read(self, user_id: str):  # type: ignore
        response: dict = {}

        if self.global_root is None:
            self.global_root = response
            has_global_root: bool = False
        else:
            has_global_root = True

        if user_id == 'me':
            user_id = self.user.user_id

        mongo_query = (
            Q(main_author=user_id) | Q(reviewers=user_id) | Q(coauthors=user_id)
        )
        # self.user must have access to the upload
        if user_id != self.user.user_id and not self.user.is_admin:
            mongo_query &= (
                Q(main_author=self.user.user_id)
                | Q(reviewers=self.user.user_id)
                | Q(coauthors=self.user.user_id)
            )

        self.uploads = Upload.objects(mongo_query)
        self.entries = Entry.objects(upload_id__in=[v.upload_id for v in self.uploads])
        self.datasets = Dataset.m_def.a_mongo.objects(
            dataset_id__in=set(
                v for e in self.entries if e.datasets for v in e.datasets
            )
        )

        self._walk(
            GraphNode(
                upload_id='__NOT_NEEDED__',
                entry_id='__NOT_NEEDED__',
                current_path=[],
                result_root=response,
                ref_result_root=self.global_root,
                archive=self.retrieve_user(user_id),
                archive_root=None,
                definition=None,
                visited_path=set(),
                current_depth=0,
                reader=self,
            ),
            self.required_query,
            self.global_config,
        )

        self._populate_error_list(response)

        if not has_global_root:
            self.global_root = None

        return response

    @classmethod
    def validate_config(cls, key: str, config: RequestConfig):
        if config.query is not None:
            raise ConfigError('User reader does not support query.')
        if config.pagination is not None:
            raise ConfigError('User reader does not support pagination.')

        return MongoReader.validate_config(key, config)


class FileSystemReader(GeneralReader):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._root_path: list = []

    def read(self, upload_id: str, path: str = None) -> dict:
        self._root_path = [v for v in path.split('/') if v] if path else []

        response: dict = {}

        if self.global_root is None:
            self.global_root = response
            has_global_root: bool = False
        else:
            has_global_root = True

        try:
            upload: Upload = get_upload_with_read_access(
                upload_id, self.user, include_others=True
            )
        except HTTPException:
            self._log(
                f'Current user does not have access to upload {upload_id}.',
                error_type=QueryError.NOACCESS,
            )
        else:
            self._walk(
                GraphNode(
                    upload_id=upload_id,
                    entry_id='__NOT_NEEDED__',
                    current_path=[],
                    result_root=response,
                    ref_result_root=self.global_root,
                    archive=upload.upload_files,
                    archive_root=None,
                    definition=None,
                    visited_path=set(),
                    current_depth=0,
                    reader=self,
                ),
                self.required_query,
                self.global_config,
            )

        self._populate_error_list(response)

        if not has_global_root:
            self.global_root = None

        return response

    def _walk(
        self,
        node: GraphNode,
        required: dict | RequestConfig,
        parent_config: RequestConfig,
    ):
        if isinstance(required, RequestConfig):
            return self._resolve(node, required)

        if GeneralReader.__CONFIG__ in required:
            # resolve the current tree if config is present
            # excluding explicitly assigned keys
            current_config: RequestConfig = required[GeneralReader.__CONFIG__]
            self._resolve(node, current_config, omit_keys=required.keys())
        else:
            current_config = parent_config

        full_path: list = self._root_path + node.current_path
        full_path_str: str = '/'.join(full_path)
        is_current_path_file: bool = node.archive.raw_path_is_file(full_path_str)

        if not is_current_path_file:
            _populate_result(node.result_root, full_path + ['m_is'], 'Directory')

        if Token.ENTRY in required:
            # implicit resolve
            if is_current_path_file and (
                results := self._offload(
                    node.upload_id, full_path_str, required[Token.ENTRY], current_config
                )
            ):
                _populate_result(node.result_root, full_path + [Token.ENTRY], results)

        for key, value in required.items():
            if key == GeneralReader.__CONFIG__:
                continue

            child_path: list = node.current_path + [key]

            if not node.archive.raw_path_exists('/'.join(self._root_path + child_path)):
                continue

            self._walk(
                node.replace(
                    current_path=child_path,
                    current_depth=node.current_depth + 1,
                ),
                value,
                current_config,
            )

    def _resolve(
        self,
        node: GraphNode,
        config: RequestConfig,
        *,
        omit_keys=None,
        wildcard: bool = False,
    ):
        # at the point, it is guaranteed that the current path exists, but it could be a relative path
        full_path: list = self._root_path + node.current_path
        abs_path: list = []
        # condense the path
        for p in full_path:
            for pp in p.split('/'):  # to consider '../../../'
                if pp in ('.', ''):
                    continue
                if pp == '..':
                    abs_path.pop()
                else:
                    abs_path.append(pp)

        os_path: str = '/'.join(abs_path)
        if not node.archive.raw_path_is_file(os_path):
            _populate_result(node.result_root, full_path + ['m_is'], 'Directory')
            # _populate_result(
            #     node.result_root, full_path + [Token.RESPONSE, 'pagination'],
            #     config.pagination.dict() if config.pagination is not None else dict(page=1, page_size=10))

        ref_path = ['/'.join(self._root_path)]
        if ref_path[0]:
            ref_path += node.current_path
        else:
            ref_path = node.current_path

        file: RawPathInfo
        for file in node.archive.raw_directory_list(
            os_path, recursive=True, depth=config.depth if config.depth else -1
        ):
            if not config.if_include(file.path):
                continue

            results = file._asdict()
            results.pop('access', None)
            results['m_is'] = 'File' if results.pop('is_file') else 'Directory'
            if omit_keys is None or all(
                not file.path.endswith(os.path.sep + k) for k in omit_keys
            ):
                if config.directive is DirectiveType.resolved and (
                    resolved := self._offload(node.upload_id, file.path, config, config)
                ):
                    results[Token.ENTRY] = resolved

            # need to consider the relative path and the absolute path conversion
            file_path: list = [
                v for v in file.path.split('/') if v
            ]  # path from upload root

            if not (result_path := ref_path + file_path[len(abs_path) :]):
                result_path = [file_path[-1]]

            _populate_result(node.result_root, result_path, results, path_like=True)

    @classmethod
    def validate_config(cls, key: str, config: RequestConfig):
        try:
            if config.pagination is not None:
                config.pagination = Pagination.parse_obj(config.pagination)
        except Exception as e:
            raise ConfigError(str(e))

        return config

    def _offload(
        self, upload_id: str, main_file: str, required, parent_config: RequestConfig
    ) -> dict:
        if entry := Entry.objects(upload_id=upload_id, mainfile=main_file).first():
            with EntryReader(
                required,
                user=self.user,
                init=False,
                config=parent_config,
                global_root=self.global_root,
            ) as reader:
                return reader.read(entry.entry_id)
        return {}


class ArchiveReader(GeneralReader):
    """
    This class provides functionalities to read an archive with the required fields.
    A sample query will look like the following.

    .. code-block:: python
    {
        "results": { "m_request": RequestConfigInstance },
        "workflow": {
            "m_request": RequestConfigInstance,
            "calculation_result_ref": {
                "system_ref": { "m_request": RequestConfigInstance }
            }
        }
    }

    Usage:
    Since the reader needs to cache files, it is important to call the close method on exit.
    This can be done in three ways:
        1. Use plain create-read-close. Catch exceptions by yourself.
            >>> query = {}
            >>> user = {}
            >>> archive = {}
            >>> reader = ArchiveReader(query, user)
            >>> result = reader.read(archive)
            >>> reader.close() # important
        2. Use context manager.
            >>> with ArchiveReader(query, user) as reader:
            >>>     result = reader.read(archive)
        3. Use static method.
            >>> result = ArchiveReader.read_required(query, user, archive)
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.package_pool: dict = {}

    @staticmethod
    def __if_strip(node: GraphNode, config: RequestConfig):
        if (
            config.max_list_size is not None
            and isinstance(node.archive, list)
            and len(node.archive) > config.max_list_size
        ):
            return True

        if (
            config.max_dict_size is not None
            and isinstance(node.archive, dict)
            and len(node.archive) > config.max_dict_size
        ):
            return True

        if config.depth is not None and node.current_depth > config.depth:
            return True

        return False

    def read(self, *args) -> dict:
        """
        Read the given archive with the required fields.
        Takes two forms of arguments:
            1. archive: dict | ArchiveDict
            2. upload_id: str, entry_id: str
        """
        archive = args[0] if len(args) == 1 else self.load_archive(*args)

        metadata = archive['metadata']

        response: dict = {}

        if self.global_root is None:
            self.global_root = response
            has_global_root: bool = False
        else:
            has_global_root = True

        self._walk(
            GraphNode(
                upload_id=metadata['upload_id'],
                entry_id=metadata['entry_id'],
                current_path=[],
                result_root=response,
                ref_result_root=self.global_root,
                archive=archive,
                archive_root=archive,
                definition=EntryArchive.m_def,
                visited_path=set(),
                current_depth=0,
                reader=self,
            ),
            self.required_query,
            self.global_config,
        )

        self._populate_error_list(response)

        if not has_global_root:
            self.global_root = None

        return response

    def _walk(
        self,
        node: GraphNode,
        required: dict | RequestConfig,
        parent_config: RequestConfig,
    ):
        """
        Walk through the archive according to the required query.
        The parent config is passed down to the children in case there is no config in any subtree.
        """
        if isinstance(required, RequestConfig):
            return self._resolve(node, required)

        if required.pop(GeneralReader.__WILDCARD__, None):
            self._log(
                "Wildcard '*' as field name is not supported in archive query as its data is not homogeneous",
                error_type=QueryError.NOTFOUND,
            )

        current_config: RequestConfig = required.get(
            GeneralReader.__CONFIG__, parent_config
        )

        # if it is a subtree, itself needs to be resolved
        if GeneralReader.__CONFIG__ in required:
            # keys explicitly given will be resolved later on during tree traversal
            # thus omit here to avoid duplicate resolve
            self._resolve(node, current_config, omit_keys=required.keys())

        # update node definition if required
        node = self._check_definition(node, current_config)
        # in case of a reference, resolve it implicitly
        node = self._check_reference(node, current_config, implicit_resolve=True)

        # walk through the required fields
        for key, value in required.items():
            if key == GeneralReader.__CONFIG__:
                continue

            if key == Token.DEF:
                with DefinitionReader(
                    value,
                    user=self.user,
                    init=False,
                    config=current_config,
                    global_root=self.global_root,
                ) as reader:
                    _populate_result(
                        node.result_root,
                        node.current_path + [Token.DEF],
                        reader.read(node.definition),
                    )
                continue

            # key may contain index, cached
            name, index = _parse_key(key)

            try:
                child_archive = node.archive.get(name, None)
            except AttributeError as e:
                # implicit resolve failed, or wrong path given
                self._log(str(e), error_type=QueryError.NOTFOUND)
                continue

            # this may be a dict, a list, or a primitive value
            if child_archive is None:
                self._log(
                    f'Field {name} is not found in archive {node.generate_reference()}.',
                    error_type=QueryError.NOTFOUND,
                )
                continue

            child_definition = node.definition.all_properties.get(name, None)
            if child_definition is None:
                self._log(
                    f'Definition {name} is not found.', error_type=QueryError.NOTFOUND
                )
                continue

            from nomad.archive.storage_v2 import ArchiveList as ArchiveListNew

            is_list: bool = isinstance(
                child_archive, (list, ArchiveList, ArchiveListNew)
            )

            if (
                is_list
                and isinstance(child_definition, SubSection)
                and not child_definition.repeats
            ):
                self._log(f'Definition {key} is not repeatable.')
                continue

            child_path: list = node.current_path + [name]

            child = functools.partial(node.replace, definition=child_definition)

            if isinstance(value, RequestConfig):
                # this is a leaf, resolve it according to the config
                self._resolve(
                    child(current_path=child_path, archive=child_archive), value
                )
            elif isinstance(value, dict):
                # this is a nested query, keep walking down the tree
                def __walk(__path, __archive):
                    self._walk(
                        child(current_path=__path, archive=__archive),
                        value,
                        current_config,
                    )

                if is_list:
                    # field[start:end]: dict
                    for i in _normalise_index(index, len(child_archive)):
                        __walk(child_path + [str(i)], child_archive[i])
                else:
                    # field: dict
                    __walk(child_path, child_archive)
            elif isinstance(value, list):
                # optionally support alternative syntax
                pass
            else:
                # should never reach here
                raise ConfigError(f'Invalid required config: {value}.')

    def _resolve(
        self,
        node: GraphNode,
        config: RequestConfig,
        *,
        omit_keys=None,
        wildcard: bool = False,
    ):
        """
        Resolve the given node.

        If omit_keys is given, the keys matching any of the omit_keys will not be resolved.
        Those come from explicitly given fields in the required query.
        They are handled by the caller.
        """
        from nomad.archive.storage_v2 import ArchiveList as ArchiveListNew

        if isinstance(node.archive, (list, ArchiveList, ArchiveListNew)):
            return self._resolve_list(node, config)

        # no matter if to resolve, it is always necessary to replace the definition with potential custom definition
        node = self._check_definition(node, config)
        # if it needs to resolve, it is necessary to check references
        node = self._check_reference(
            node, config, implicit_resolve=omit_keys is not None
        )

        from nomad.archive.storage_v2 import ArchiveDict as ArchiveDictNew

        if not isinstance(node.archive, (dict, ArchiveDict, ArchiveDictNew)):
            # primitive type data is always included
            # this is not affected by size limit nor by depth limit
            _populate_result(
                node.result_root, node.current_path, self._apply_resolver(node, config)
            )
            return

        if getattr(node.definition, 'type', None) in (Any, JSON) or isinstance(
            node.definition, Quantity
        ):
            # the container size limit does not recursively apply to JSON
            result_to_write = (
                f'__INTERNAL__:{node.generate_reference()}'
                if self.__if_strip(node, config)
                else self._apply_resolver(node, config)
            )
            _populate_result(node.result_root, node.current_path, result_to_write)
            return

        for key in sorted(node.archive.keys()):
            if key == Token.DEF:
                continue

            if config.if_include(key) and (
                omit_keys is None or all(not k.startswith(key) for k in omit_keys)
            ):
                child_definition = node.definition.all_properties.get(key, None)

                if child_definition is None:
                    self._log(f'Definition {key} is not found.')
                    continue

                if isinstance(child_definition, SubSection):
                    child_definition = child_definition.sub_section

                child_node = node.replace(
                    archive=node.archive[key],
                    current_path=node.current_path + [key],
                    definition=child_definition,
                    current_depth=node.current_depth + 1,
                )

                if self.__if_strip(child_node, config):
                    _populate_result(
                        node.result_root,
                        child_node.current_path,
                        f'__INTERNAL__:{child_node.generate_reference()}',
                    )
                    continue

                self._resolve(
                    child_node,
                    config.new(
                        {
                            'property_name': key,  # set the proper quantity name
                            'include': ['*'],  # ignore the pattern for children
                            'exclude': None,
                            'index': None,  # ignore index requirements for children
                        }
                    ),
                )

    def _check_definition(self, node: GraphNode, config: RequestConfig) -> GraphNode:
        """
        Check the existence of custom definition.
        If positive, overwrite the corresponding information of the current node.
        """
        from nomad.archive.storage_v2 import ArchiveDict as ArchiveDictNew

        if not isinstance(node.archive, (dict, ArchiveDict, ArchiveDictNew)):
            return node

        def __if_contains(m_def):
            return _if_exists(
                self.global_root, _convert_ref_to_path(m_def.strict_reference())
            )

        custom_def: str | None = node.archive.get('m_def', None)
        custom_def_id: str | None = node.archive.get('m_def_id', None)
        if custom_def is None and custom_def_id is None:
            if config.include_definition is DefinitionType.both:
                definition = node.definition
                if isinstance(definition, SubSection):
                    definition = definition.sub_section.m_resolved()
                if not __if_contains(definition):
                    with DefinitionReader(
                        RequestConfig(directive=DirectiveType.plain),
                        user=self.user,
                        init=False,
                        config=config,
                        global_root=self.global_root,
                    ) as reader:
                        _populate_result(
                            node.result_root,
                            node.current_path + [Token.DEF],
                            reader.read(definition),
                        )
            return node

        try:
            new_def = self._retrieve_definition(custom_def, custom_def_id, node)
        except Exception as e:
            self._log(
                f'Failed to retrieve definition: {e}', error_type=QueryError.NOTFOUND
            )
            return node

        if config.include_definition is not DefinitionType.none and not __if_contains(
            new_def
        ):
            with DefinitionReader(
                RequestConfig(directive=DirectiveType.plain),
                user=self.user,
                init=False,
                config=config,
                global_root=self.global_root,
            ) as reader:
                _populate_result(
                    node.result_root,
                    node.current_path + [Token.DEF],
                    reader.read(new_def),
                )

        return node.replace(definition=new_def)

    def _check_reference(
        self, node: GraphNode, config: RequestConfig, *, implicit_resolve: bool = False
    ) -> GraphNode:
        """
        Check the existence of custom definition.
        If positive, overwrite the corresponding information of the current node.
        """
        original_def = node.definition

        # resolve subsections
        if isinstance(original_def, SubSection):
            return node.replace(definition=original_def.sub_section.m_resolved())

        # if not a quantity reference, early return
        if not (
            isinstance(original_def, Quantity)
            and isinstance(original_def.type, Reference)
        ):
            return node

        # for quantity references, early return if no need to resolve
        if not implicit_resolve and config.directive is not DirectiveType.resolved:
            return node

        assert isinstance(node.archive, str), 'A reference string is expected.'

        # maximum resolve depth reached, do not resolve further
        if config.resolve_depth and len(node.visited_path) == config.resolve_depth:
            return node

        try:
            resolved_node = node.goto(node.archive, config.resolve_inplace)
        except ArchiveError as e:
            # cannot resolve somehow
            # treat it as a normal string
            # populate to the result
            self._log(str(e), error_type=QueryError.ARCHIVEERROR)
            _populate_result(node.result_root, node.current_path, node.archive)
            return node

        ref = original_def.type
        target = (
            ref.target_quantity_def
            if isinstance(ref, QuantityReference)
            else ref.target_section_def
        )
        # need to check custom definition again since the referenced archive may have a custom definition
        return self._check_definition(
            resolved_node.replace(definition=target.m_resolved()), config
        )

    # noinspection PyUnusedLocal
    def _retrieve_definition(
        self, m_def: str | None, m_def_id: str | None, node: GraphNode
    ):
        # todo: more flexible definition retrieval, accounting for definition id, mismatches, etc.
        context = ServerContext(
            get_upload_with_read_access(node.upload_id, self.user, include_others=True)
        )

        def __resolve_definition_in_archive(
            _root: dict,
            _path_stack: list,
            _upload_id: str = None,
            _entry_id: str = None,
        ):
            cache_key = f'{_upload_id}:{_entry_id}'

            if cache_key not in self.package_pool:
                custom_def_package: Package = Package.m_from_dict(
                    _root, m_context=context
                )
                # package loaded in this way does not have an attached archive
                # we manually set the upload_id and entry_id so that
                # correct references can be generated in the corresponding method
                custom_def_package.entry_id = _entry_id
                custom_def_package.upload_id = _upload_id
                custom_def_package.init_metainfo()
                self.package_pool[cache_key] = custom_def_package

            return self.package_pool[cache_key].m_resolve_path(_path_stack)

        if m_def is not None:
            if m_def.startswith(('#/', '/')):
                # appears to be a local definition
                return __resolve_definition_in_archive(
                    to_json(node.archive_root['definitions']),
                    [v for v in m_def.split('/') if v not in ('', '#', 'definitions')],
                    node.archive_root['metadata']['upload_id'],
                    node.archive_root['metadata']['entry_id'],
                )
            # todo: !!!need to unify different formats!!!
            # check if m_def matches the pattern 'entry_id:example_id.example_section.example_quantity'
            regex = re.compile(r'entry_id:(.+)(?:\.(.+))+')
            if match := regex.match(m_def):
                entry_id = match.groups()[0]
                upload_id = Entry.objects(entry_id=entry_id).first().upload_id
                archive = self.load_archive(upload_id, entry_id)
                return __resolve_definition_in_archive(
                    to_json(archive['definitions']),
                    list(match.groups()[1:]),
                    upload_id,
                    entry_id,
                )

        # further consider when only m_def_id is given, etc.

        # this is not likely to be reached
        # it does not work anyway
        proxy = SectionReference.deserialize(None, None, m_def)
        proxy.m_proxy_context = context
        return proxy.section_cls.m_def

    @classmethod
    def validate_config(cls, key: str, config: RequestConfig):
        if config.pagination is not None:
            raise ConfigError(f'Pagination is not supported in {cls.__name__} @ {key}.')
        if config.query is not None:
            raise ConfigError(f'Query is not supported in {cls.__name__} @ {key}.')

        return config

    @staticmethod
    def read_required(
        archive: ArchiveDict | dict, required_query: dict | str, user=None
    ):
        """
        A helper wrapper.
        """
        with ArchiveReader(required_query, user=user) as reader:
            return reader.read(archive)


class DefinitionReader(GeneralReader):
    def read(self, archive: Definition) -> dict:
        response: dict = {Token.DEF: {}}

        if self.global_root is None:
            self.global_root = response
            has_global_root: bool = False
        else:
            has_global_root = True

        self._walk(
            GraphNode(
                upload_id='__NONE__',
                entry_id='__NONE__',
                current_path=[Token.DEF],
                result_root=response,
                ref_result_root=self.global_root,
                archive=archive,
                archive_root=None,
                definition=None,
                visited_path=set(),
                current_depth=0,
                reader=self,
            ),
            self.required_query,
            self.global_config,
        )

        self._populate_error_list(response)

        if not has_global_root:
            self.global_root = None

        return response

    def _walk(
        self,
        node: GraphNode,
        required: dict | RequestConfig,
        parent_config: RequestConfig,
    ):
        if isinstance(required, RequestConfig):
            return self._resolve(
                self._switch_root(node, inplace=required.resolve_inplace), required
            )

        current_config: RequestConfig = required.get(
            GeneralReader.__CONFIG__, parent_config
        )

        node = self._switch_root(node, inplace=current_config.resolve_inplace)

        # if it is a subtree, itself needs to be resolved
        if GeneralReader.__CONFIG__ in required:
            # keys explicitly given will be resolved later on during tree traversal
            # thus omit here to avoid duplicate resolve
            self._resolve(node, current_config, omit_keys=required.keys())

        def __convert(m_def):
            return _convert_ref_to_path_string(m_def.strict_reference())

        for key, value in required.items():
            if key == GeneralReader.__CONFIG__:
                continue

            # key may contain index, cached
            name, index = _parse_key(key)

            child_def = getattr(node.archive, name, None)
            if child_def is None:
                continue

            is_list: bool = isinstance(child_def, MSubSectionList)

            # for derived quantities like 'all_properties', 'all_quantities', etc.
            # normalise them to maps
            is_plain_container: bool = (
                False if is_list else isinstance(child_def, (list, set, dict))
            )
            if is_plain_container and isinstance(child_def, (list, set)):
                child_def = {v.name: v for v in child_def}

            child_path: list = node.current_path + [name]

            # to avoid infinite loop
            # put a reference string here and skip it later
            if is_list:
                for i in _normalise_index(index, len(child_def)):
                    if child_def[i] is not node.archive:
                        continue
                    _populate_result(
                        node.result_root, child_path + [str(i)], __convert(child_def[i])
                    )
                    break  # early return assuming children do not repeat
            elif is_plain_container:
                # this is a derived quantity like 'all_properties', 'all_quantities', etc.
                # just write reference strings to the corresponding paths
                # whether they shall be resolved or not is determined by the config and will be handled later
                for k, v in child_def.items():
                    _populate_result(node.result_root, child_path + [k], __convert(v))
            elif child_def is node.archive:
                assert isinstance(child_def, Definition)
                _populate_result(node.result_root, child_path, __convert(child_def))

            if isinstance(value, RequestConfig):
                # this is a leaf, resolve it according to the config
                def __resolve(__path, __archive):
                    if __archive is node.archive:
                        return
                    self._resolve(
                        self._switch_root(
                            node.replace(current_path=__path, archive=__archive),
                            inplace=value.resolve_inplace,
                        ),
                        value,
                    )

                if is_list:
                    for i in _normalise_index(index, len(child_def)):
                        __resolve(child_path + [str(i)], child_def[i])
                elif is_plain_container:
                    if value.directive is DirectiveType.resolved:
                        for k, v in child_def.items():
                            __resolve(child_path + [k], v)
                else:
                    __resolve(child_path, child_def)
            elif isinstance(value, dict):
                # this is a nested query, keep walking down the tree
                def __walk(__path, __archive):
                    if __archive is node.archive:
                        return
                    self._walk(
                        node.replace(current_path=__path, archive=__archive),
                        value,
                        current_config,
                    )

                if is_list:
                    # field[start:end]: dict
                    for i in _normalise_index(index, len(child_def)):
                        __walk(child_path + [str(i)], child_def[i])
                elif is_plain_container:
                    for k, v in child_def.items():
                        __walk(child_path + [k], v)
                else:
                    __walk(child_path, child_def)
            elif isinstance(value, list):
                # optionally support alternative syntax
                pass
            else:
                # should never reach here
                raise ConfigError(f'Invalid required config: {value}.')

    def _resolve(
        self,
        node: GraphNode,
        config: RequestConfig,
        *,
        omit_keys=None,
        wildcard: bool = False,
    ):
        if isinstance(node.archive, list):
            return self._resolve_list(node, config)

        def __unwrap_ref(ref_type: Reference):
            """
            For a quantity reference, need to unwrap the reference to get the target definition.
            """
            return (
                ref_type.target_quantity_def
                if isinstance(ref_type, QuantityReference)
                else ref_type.target_section_def
            )

        def __override_path(q, s, v, p):
            """
            Normalise all definition identifiers with unique global reference.
            """

            def __convert(m_def):
                return _convert_ref_to_path_string(m_def.strict_reference())

            if isinstance(s, Quantity) and isinstance(v, dict):
                if isinstance(s.type, Reference):
                    v['type_data'] = __convert(__unwrap_ref(s.type))
            elif (
                isinstance(s, SubSection)
                and q.name == 'sub_section'
                and isinstance(v, str)
            ):
                v = __convert(s.sub_section)
            elif (
                isinstance(s, Section)
                and isinstance(v, str)
                and q.type is SectionReference
            ):
                v = __convert(s.m_resolve(p))

            return v

        # rewrite quantity type data with global reference
        if not self._check_cache(node.current_path, config.hash):
            self._cache_hash(node.current_path, config.hash)
            _populate_result(
                node.result_root,
                node.current_path,
                node.archive.m_to_dict(with_out_meta=True, transform=__override_path),
            )

        if isinstance(node.archive, Quantity):
            if isinstance(ref := node.archive.type, Reference):
                target = __unwrap_ref(ref)
                ref_str: str = target.strict_reference()
                path_stack: list = _convert_ref_to_path(ref_str)
                # check if it has been populated
                if ref_str not in node.visited_path and not _if_exists(
                    node.ref_result_root, path_stack
                ):
                    self._resolve(
                        node.replace(
                            archive=target,
                            current_path=path_stack,
                            result_root=node.ref_result_root,
                            visited_path=node.visited_path.union({ref_str}),
                            current_depth=node.current_depth + 1,
                        ),
                        config,
                    )
            # no need to do anything for quantity
            # as quantities do no contain additional contents to be extracted
            return

        #
        # the following is for section
        #

        # no need to recursively resolve all relevant definitions if the directive is plain
        if config.directive == DirectiveType.plain:
            return

        # for definition, use either result depth or resolve depth to limit the recursion
        if config.depth is not None and node.current_depth + 1 > config.depth:
            return
        if (
            config.resolve_depth is not None
            and len(node.visited_path) + 1 > config.resolve_depth
        ):
            return

        for name, unwrap in (
            ('base_sections', False),
            ('sub_sections', True),
            ('quantities', False),
        ):
            for index, base in enumerate(getattr(node.archive, name, [])):
                section = base.sub_section.m_resolved() if unwrap else base
                ref_str = section.strict_reference()
                path_stack = _convert_ref_to_path(ref_str)
                if section is node.archive or self._check_cache(
                    path_stack, config.hash
                ):
                    continue
                self._resolve(
                    self._switch_root(
                        node.replace(
                            archive=section,
                            current_path=node.current_path + [name, str(index)],
                            visited_path=node.visited_path.union({ref_str}),
                            current_depth=node.current_depth + 1,
                        ),
                        inplace=config.resolve_inplace,
                    ),
                    config,
                )

    @staticmethod
    def _switch_root(node: GraphNode, *, inplace: bool) -> GraphNode:
        """
        Depending on whether to resolve in place, adapt the current root of the result tree.
        If NOT in place, write a global reference string to the current place, then switch to the referenced root.
        """
        if inplace:
            return node

        ref_str: str = node.archive.strict_reference()
        if not isinstance(node.archive, Quantity):
            _populate_result(
                node.result_root,
                node.current_path,
                _convert_ref_to_path_string(ref_str),
            )

        return node.replace(
            result_root=node.ref_result_root, current_path=_convert_ref_to_path(ref_str)
        )

    @classmethod
    def validate_config(cls, key: str, config: RequestConfig):
        return config


__M_SEARCHABLE__: dict = {
    Token.SEARCH: ElasticSearchReader,
    Token.METADATA: ElasticSearchReader,
    Token.ENTRY: EntryReader,
    Token.ENTRIES: EntryReader,
    Token.UPLOAD: UploadReader,
    Token.UPLOADS: UploadReader,
    Token.USER: UserReader,
    Token.USERS: UserReader,
    Token.DATASET: DatasetReader,
    Token.DATASETS: DatasetReader,
}
