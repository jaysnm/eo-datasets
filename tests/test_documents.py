# coding=utf-8
"""
Module
"""
from __future__ import absolute_import

import pytest

from eodatasets.documents import find_metadata_path, _find_any_metadata_suffix
from tests import write_files


def test_find_metadata_path():
    files = write_files({
        'directory_dataset': {
            'file1.txt': '',
            'file2.txt': '',
            'ga-metadata.yaml.gz': ''
        },
        'file_dataset.tif': '',
        'file_dataset.tif.agdc-md.yaml': '',
        'dataset_metadata.yaml': '',
        'no_metadata.tif': '',
    })

    # A metadata file can be specified directly.
    path = find_metadata_path(files.joinpath('dataset_metadata.yaml'))
    assert path.absolute() == files.joinpath('dataset_metadata.yaml').absolute()

    # A dataset directory will have an internal 'agdc-metadata' file.
    path = find_metadata_path(files.joinpath('directory_dataset'))
    assert path.absolute() == files.joinpath('directory_dataset', 'ga-metadata.yaml.gz').absolute()

    # Other files can have a sibling file ending in 'agdc-md.yaml'
    path = find_metadata_path(files.joinpath('file_dataset.tif'))
    assert path.absolute() == files.joinpath('file_dataset.tif.agdc-md.yaml').absolute()

    # No metadata to find.
    assert find_metadata_path(files.joinpath('no_metadata.tif')) is None

    # Dataset itself doesn't exist.
    assert find_metadata_path(files.joinpath('missing-dataset.tif')) is None


def test_find_any_metatadata_suffix():
    files = write_files({
        'directory_dataset': {
            'file1.txt': '',
            'file2.txt': '',
            'agdc-metadata.json.gz': ''
        },
        'file_dataset.tif.ga-md.yaml': '',
        'dataset_metadata.YAML': '',
        'no_metadata.tif': '',
    })

    path = _find_any_metadata_suffix(files.joinpath('dataset_metadata'))
    assert path.absolute() == files.joinpath('dataset_metadata.YAML').absolute()

    path = _find_any_metadata_suffix(files.joinpath('directory_dataset', 'agdc-metadata'))
    assert path.absolute() == files.joinpath('directory_dataset', 'agdc-metadata.json.gz').absolute()

    path = _find_any_metadata_suffix(files.joinpath('file_dataset.tif.ga-md'))
    assert path.absolute() == files.joinpath('file_dataset.tif.ga-md.yaml').absolute()

    # Returns none if none exist
    path = _find_any_metadata_suffix(files.joinpath('no_metadata'))
    assert path is None