import os
from typing import Optional

import pytest

from config import Configuration


class TestAdminUI(object):
    # ENV_ADMIN_UI_PACKAGE_NAME = 'LIBRARY_REGISTRY_ADMIN_PACKAGE_NAME'
    # ENV_ADMIN_UI_PACKAGE_VERSION = 'LIBRARY_REGISTRY_ADMIN_PACKAGE_VERSION'
    # os.environ['LIBRARY_REGISTRY_ADMIN_PACKAGE_NAME']
    # os.environ['LIBRARY_REGISTRY_ADMIN_PACKAGE_VERSION']

    @staticmethod
    def set_env(key: str, value: Optional[str]):
        if value:
            os.environ[key] = value
        elif key in os.environ:
            del os.environ[key]

    # TODO: No test coverage for OSes with different path separator.
    @pytest.mark.parametrize(
        'package_name, package_version, for_dev_mode, expected_result',
        [
            [None, None, False,
             'https://cdn.jsdelivr.net/npm/@thepalaceproject/library-registry-admin'],
            ['@some-scope/some-package', '1.0.0', False,
             'https://cdn.jsdelivr.net/npm/@some-scope/some-package@1.0.0'],
            ['some-package', '1.0.0', False,
             'https://cdn.jsdelivr.net/npm/some-package@1.0.0'],
            [None, None, True,
             'node_modules/@thepalaceproject/library-registry-admin'],
            [None, '1.0.0', True,
             'node_modules/@thepalaceproject/library-registry-admin'],
        ])
    def test_admin_ui_package_rel(self, package_name: Optional[str], package_version: Optional[str],
                                  for_dev_mode: bool, expected_result: str):
        self.set_env('LIBRARY_REGISTRY_ADMIN_PACKAGE_NAME', package_name)
        self.set_env('LIBRARY_REGISTRY_ADMIN_PACKAGE_VERSION', package_version)
        result = Configuration.admin_ui_package_rel(development=for_dev_mode)
        assert result == expected_result

    @pytest.mark.parametrize(
        'package_name, package_version, for_dev_mode, expected_result',
        [
            [None, None, False,
             'https://cdn.jsdelivr.net/npm/@thepalaceproject/library-registry-admin'],
            [None, None, True,
             '/my-base-dir/node_modules/@thepalaceproject/library-registry-admin'],
        ])
    def test_admin_ui_package_abs(self, package_name: Optional[str], package_version: Optional[str],
                                  for_dev_mode: bool, expected_result: str):
        self.set_env('LIBRARY_REGISTRY_ADMIN_PACKAGE_NAME', package_name)
        self.set_env('LIBRARY_REGISTRY_ADMIN_PACKAGE_VERSION', package_version)
        result = Configuration.admin_ui_package_abs(development=for_dev_mode, base_dir='/my-base-dir')
        assert result == expected_result
