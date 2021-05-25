from collections import namedtuple
import contextlib
import copy
import json
import os
import logging

@contextlib.contextmanager
def temp_config(new_config=None, replacement_classes=None):
    old_config = Configuration.instance
    replacement_classes = replacement_classes or [Configuration]
    if new_config is None:
        new_config = copy.deepcopy(old_config)
    try:
        for c in replacement_classes:
            c.instance = new_config
        yield new_config
    finally:
        for c in replacement_classes:
            c.instance = old_config

class CannotLoadConfiguration(Exception):
    pass

class Configuration(object):

    instance = None

    APP_BASE_DIRECTORY = os.path.abspath(os.path.dirname(__file__))

    # Environment variables that contain URLs to the database
    DATABASE_TEST_ENVIRONMENT_VARIABLE = 'SIMPLIFIED_TEST_DATABASE'
    DATABASE_PRODUCTION_ENVIRONMENT_VARIABLE = 'SIMPLIFIED_PRODUCTION_DATABASE'

    # Environment variables that contain admin client package information.
    ENV_ADMIN_UI_PACKAGE_NAME = 'LIBRARY_REGISTRY_ADMIN_PACKAGE_NAME'
    ENV_ADMIN_UI_PACKAGE_VERSION = 'LIBRARY_REGISTRY_ADMIN_PACKAGE_VERSION'

    ADMIN_UI_DEFAULT_PACKAGE_NAME = '@thepalaceproject/library-registry-admin'

    ADMIN_UI_CDN_PACKAGE_TEMPLATE = 'https://cdn.jsdelivr.net/npm/{name}{version}'
    ADMIN_UI_DEV_PACKAGE_TEMPLATE = 'node_modules/{name}'
    ADMIN_UI_ASSET_LOCATION_REL_TEMPLATE = 'static/{asset}'

    log = logging.getLogger("Configuration file loader")

    INTEGRATIONS = 'integrations'

    BASE_URL = 'base_url'

    ADOBE_VENDOR_ID = "vendor_id"
    ADOBE_VENDOR_ID_NODE_VALUE = "node_value"
    ADOBE_VENDOR_ID_DELEGATE_URL = "delegate_url"

    # The URL to the document containing the terms of service for
    # library registration
    REGISTRATION_TERMS_OF_SERVICE_URL = "registration_terms_of_service_url"

    # An HTML snippet describing the terms of service for library
    # registration. It's better if this is a short snippet of text
    # with a link, rather than the actual text of the terms of
    # service.
    REGISTRATION_TERMS_OF_SERVICE_HTML = "registration_terms_of_service_html"

    # Email sent by the library registry will be 'from' this address,
    # and receipients will be invited to contact this address if they
    # have problems.
    REGISTRY_CONTACT_EMAIL = "registry_contact_email"

    # If the registry provides access to a web-based client, it can
    # specify the URL with this setting. The URL must be templated and contain
    # a `{uuid}` expression, to provide the web URL for a specific library.
    WEB_CLIENT_URL = "web_client_url"

    # If a library references a place that's not explicitly in any particular
    # nation, we assume that they're talking about this nation.
    DEFAULT_NATION_ABBREVIATION = "default_nation_abbreviation"

    # For performance reasons, a registry may want to omit certain
    # pieces of information from large feeds. This sitewide setting
    # controls how big a feed must be to be considered 'large'.
    LARGE_FEED_SIZE = "large_feed_size"

    # The name of the sitewide secret used for admin login.
    SECRET_KEY = "secret_key"

    @classmethod
    def database_url(cls, test=False):
        """Find the URL to the database so that other configuration
        settings can be looked up.
        """
        if test:
            environment_variable = cls.DATABASE_TEST_ENVIRONMENT_VARIABLE
        else:
            environment_variable = cls.DATABASE_PRODUCTION_ENVIRONMENT_VARIABLE

        url = os.environ.get(environment_variable)
        if not url:
            raise CannotLoadConfiguration(
                "Database URL was not defined in environment variable (%s) or configuration file." % environment_variable
            )
        return url

    @classmethod
    def vendor_id(cls, _db):
        """Look up the Adobe Vendor ID configuration for this registry.

        :return: a 3-tuple (vendor ID, node value, [delegates])
        """
        from model import ExternalIntegration

        integration = ExternalIntegration.lookup(
            _db, ExternalIntegration.ADOBE_VENDOR_ID,
            ExternalIntegration.DRM_GOAL)
        if not integration:
            return None, None, []
        setting = integration.setting(cls.ADOBE_VENDOR_ID_DELEGATE_URL)
        delegates = []
        try:
            delegates = setting.json_value or []
        except ValueError as e:
            cls.log.warn("Invalid Adobe Vendor ID delegates configured.")

        node = integration.setting(cls.ADOBE_VENDOR_ID_NODE_VALUE).value
        if node:
            node = int(node, 16)
        return (
            integration.setting(cls.ADOBE_VENDOR_ID).value,
            node, delegates,
        )

    @classmethod
    def admin_ui_asset_file(cls, asset_type: str, *, development=False) -> str:
        """Get the HTML reference for a file, suitable for a link or script element.

        :param asset_type: The filetype of the asset. For example,
            "js" or "css".
        :param development: Boolean indicating whether (True) or not (False)
            we're seeking the development or production location.
        :return: An HTML reference to the file.
        """
        # Assuming here that names of js and css assets will follow package
        # name. This might become incorrect at some point in the future.
        name = (os.environ.get(cls.ENV_ADMIN_UI_PACKAGE_NAME) or cls.ADMIN_UI_DEFAULT_PACKAGE_NAME)
        # Node package name might have a scope.
        filename = name.split('/')[-1]
        asset_path = '/static'
        if not development:
            asset_path = os.path.join(cls.admin_ui_package_abs(development=development), 'dist')
        return os.path.join(asset_path, f'{filename}.{asset_type.lower()}')

    @classmethod
    def admin_ui_package_rel(cls, *, development=False) -> str:
        """Compute the location (URL or path) for the admin UI package or specified asset type.

        :param asset_type: The filetype of the asset. For example,
            "js" or "css".
        :param development: Boolean indicating whether (True) or not (False)
            we're seeking the development or production location.

        :return: String representation of the URL/path for either the asset
            of the given type or, if no type is specified, the base path
            of the package.
        """
        template = (cls.ADMIN_UI_DEV_PACKAGE_TEMPLATE
                    if development
                    else cls.ADMIN_UI_CDN_PACKAGE_TEMPLATE)

        name = (os.environ.get(cls.ENV_ADMIN_UI_PACKAGE_NAME) or
                cls.ADMIN_UI_DEFAULT_PACKAGE_NAME)
        version = os.environ.get(cls.ENV_ADMIN_UI_PACKAGE_VERSION)
        version = f'@{version}' if version else ''

        return template.format(name=name, version=version)

    @classmethod
    def admin_ui_package_abs(cls, *, development=False, base_dir=APP_BASE_DIRECTORY) -> str:
        location = cls.admin_ui_package_rel(development=development)
        if development:
            location = os.path.join(base_dir, location)
        return location
