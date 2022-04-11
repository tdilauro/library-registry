import json
import logging
import time
from smtplib import SMTPException
from urllib.parse import unquote

import flask
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_OAEP
from flask import (Response, render_template_string, request, url_for)
from flask_babel import lazy_gettext as _
from sqlalchemy.orm import (defer, joinedload)

from library_registry.drm.controller import AdobeVendorIDController
from library_registry.authentication_document import AuthenticationDocument
from library_registry.constants import (
    OPENSEARCH_MEDIA_TYPE,
    OPDS_CATALOG_REGISTRATION_MEDIA_TYPE,
)
from library_registry.emailer import Emailer
from library_registry.model import (
    ConfigurationSetting,
    Hyperlink,
    Library,
    Place,
    Resource,
    ServiceArea,
    Validation,
    production_session,
)
from library_registry.admin.controller import ViewController
from library_registry.admin.controller import AdminController
from library_registry.model_helpers import (get_one, get_one_or_create)
from library_registry.config import (Configuration, CannotLoadConfiguration, CannotSendEmail)
from library_registry.opds import (Annotator, OPDSCatalog)
from library_registry.registrar import LibraryRegistrar
from library_registry.util.app_server import (HeartbeatController, catalog_response)
from library_registry.util.http import HTTP
from library_registry.util.problem_detail import ProblemDetail
from library_registry.util.string_helpers import (base64, random_string)
from library_registry.problem_details import (
    AUTHENTICATION_FAILURE,
    INTEGRATION_ERROR,
    LIBRARY_NOT_FOUND,
    NO_AUTH_URL,
    UNABLE_TO_NOTIFY,
)


class LibraryRegistry:

    def __init__(self, _db=None, testing=False, emailer_class=Emailer):

        self.log = logging.getLogger("Library registry web app")

        if _db is None and not testing:
            _db = production_session()
        self._db = _db

        self.testing = testing

        self.setup_controllers(emailer_class)

    def setup_controllers(self, emailer_class=Emailer):
        """Set up all the controllers that will be used by the web app."""
        self.view_controller = ViewController(self)
        self.admin_controller = AdminController(self)
        self.registry_controller = LibraryRegistryController(
            self, emailer_class
        )
        self.validation_controller = ValidationController(self)
        self.coverage_controller = CoverageController(self)
        self.static_files = StaticFileController(self)
        self.heartbeat = HeartbeatController()
        vendor_id, node_value, delegates = Configuration.vendor_id(self._db)
        if vendor_id:
            self.adobe_vendor_id = AdobeVendorIDController(
                self._db, vendor_id, node_value, delegates
            )
        else:
            self.adobe_vendor_id = None

    def url_for(self, view, *args, **kwargs):
        kwargs['_external'] = True
        return url_for(view, *args, **kwargs)


class LibraryRegistryAnnotator(Annotator):

    def __init__(self, app):
        self.app = app

    def annotate_catalog(self, catalog, live=True):
        """Add links and metadata to every catalog."""
        if live:
            search_controller = "libr.search"
        else:
            search_controller = "libr.search_qa"
        search_url = self.app.url_for(search_controller)
        catalog.add_link_to_catalog(
            catalog.catalog, href=search_url, rel="search", type=OPENSEARCH_MEDIA_TYPE
        )
        register_url = self.app.url_for("libr.register")
        catalog.add_link_to_catalog(
            catalog.catalog, href=register_url, rel="register", type=OPDS_CATALOG_REGISTRATION_MEDIA_TYPE
        )

        # Add a templated link for getting a single library's entry.
        library_url = unquote(self.app.url_for("libr.library", uuid="{uuid}"))
        catalog.add_link_to_catalog(
            catalog.catalog,
            href=library_url,
            rel="http://librarysimplified.org/rel/registry/library",
            type=OPDSCatalog.OPDS_TYPE,
            templated=True
        )

        vendor_id, ignore, ignore = Configuration.vendor_id(self.app._db)
        catalog.catalog["metadata"]["adobe_vendor_id"] = vendor_id


class BaseController:

    def __init__(self, app):
        self.app = app
        self._db = self.app._db

    def library_for_request(self, uuid):
        """Look up the library the user is trying to access."""
        if not uuid:
            return LIBRARY_NOT_FOUND
        if not uuid.startswith("urn:uuid:"):
            uuid = "urn:uuid:" + uuid
        library = Library.for_urn(self._db, uuid)
        if not library:
            return LIBRARY_NOT_FOUND
        request.library = library
        return library


# This static_file function is used only when the app is running locally *without* Docker.
# In all other cases, nginx serves the static files (see docker/nginx.conf).
class StaticFileController(BaseController):
    def static_file(self, directory, filename):
        return flask.send_from_directory(directory, filename, cache_timeout=None)


class LibraryRegistryController(BaseController):

    OPENSEARCH_TEMPLATE = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">'
        '<ShortName>%(name)s</ShortName>'
        '<Description>%(description)s</Description>'
        '<Tags>%(tags)s</Tags>'
        '<Url type="application/atom+xml;profile=opds-catalog" template="%(url_template)s"/>'
        '</OpenSearchDescription>'
    )

    def __init__(self, app, emailer_class=Emailer):
        super(LibraryRegistryController, self).__init__(app)
        self.annotator = LibraryRegistryAnnotator(app)
        self.log = self.app.log
        emailer = None
        try:
            emailer = emailer_class.from_sitewide_integration(self._db)
        except CannotLoadConfiguration as e:
            self.log.error(
                "Cannot load email configuration. Will not be sending any emails.",
                exc_info=e
            )
        self.emailer = emailer

    def nearby(self, location, live=True):
        qu = Library.nearby(self._db, location, production=live)
        qu = qu.limit(5)
        if live:
            nearby_controller = 'libr.nearby'
        else:
            nearby_controller = 'libr.nearby_qa'
        this_url = self.app.url_for(nearby_controller)
        catalog = OPDSCatalog(
            self._db, str(_("Libraries near you")), this_url, qu,
            annotator=self.annotator, live=live
        )
        return catalog_response(catalog)

    def search(self, location, live=True):
        query = request.args.get('q')
        if live:
            search_controller = 'libr.search'
        else:
            search_controller = 'libr.search_qa'
        if query:
            # Run the query and send the results.
            results = Library.search(
                self._db, location, query, production=live
            )

            this_url = self.app.url_for(
                search_controller, q=query
            )
            catalog = OPDSCatalog(
                self._db, str(_('Search results for "%s"')) % query,
                this_url, results,
                annotator=self.annotator, live=live
            )
            return catalog_response(catalog)
        else:
            # Send the search form.
            body = self.OPENSEARCH_TEMPLATE % dict(
                name=_("Find your library"),
                description=_("Search by ZIP code, city or library name."),
                tags="",
                url_template=self.app.url_for(
                    search_controller) + "?q={searchTerms}"
            )
            headers = {}
            headers['Content-Type'] = OPENSEARCH_MEDIA_TYPE
            headers['Cache-Control'] = "public, no-transform, max-age: %d" % (
                3600 * 24 * 30
            )
            return Response(body, 200, headers)

    def libraries_opds(self, live=True, location=None):
        """Return all the libraries in OPDS format

        :param live: If this is True, then only production libraries are shown.
        :param location: If this is set, then libraries near this point will be
           promoted out of the alphabetical list.
        """
        alphabetical = self._db.query(Library).order_by(Library.name)

        # We always want to filter out cancelled libraries.  If live, we also filter out
        # libraries that are in the testing stage, i.e. only show production libraries.
        alphabetical = alphabetical.filter(
            Library._feed_restriction(production=live))

        # Pick up each library's hyperlinks and validation
        # information; this will save database queries when building
        # the feed.
        alphabetical = alphabetical.options(
            joinedload('hyperlinks'),
            joinedload('hyperlinks', 'resource'),
            joinedload('hyperlinks', 'resource', 'validation'),
        )
        alphabetical = alphabetical.options(defer('logo'))
        if location is None:
            # No location data is available. Use the alphabetical list as
            # the list of libraries.
            a = time.time()
            libraries = alphabetical.all()
            b = time.time()
            self.log.info(
                "Built alphabetical list of all libraries in %.2fsec" % (b-a))
        else:
            # Location data is available. Get the list of nearby libraries, then get
            # the rest of the list in alphabetical order.

            # We can't easily do the joindeload() thing for this
            # query, because it doesn't simply return Library objects,
            # but it won't return more than five results.
            a = time.time()
            nearby_libraries = Library.nearby(
                self._db, location, production=live
            ).limit(5).all()
            b = time.time()
            self.log.info("Fetched libraries near %s in %.2fsec" %
                          (location, b-a))

            # Exclude nearby libraries from the alphabetical query
            # to get a list of faraway libraries.
            faraway_libraries = alphabetical.filter(
                ~Library.id.in_([x.id for x, distance in nearby_libraries])
            )
            c = time.time()
            libraries = nearby_libraries + faraway_libraries.all()
            self.log.info("Fetched libraries far from %s in %.2fsec" %
                          (location, c-b))

        url = self.app.url_for("libr.libraries_opds")
        a = time.time()
        catalog = OPDSCatalog(
            self._db, 'Libraries', url, libraries,
            annotator=self.annotator, live=live
        )
        b = time.time()
        self.log.info("Built library catalog in %.2fsec" % (b-a))
        return catalog_response(catalog)

    def library(self):
        library = request.library
        this_url = self.app.url_for(
            'libr.library', uuid=library.internal_urn
        )
        catalog = OPDSCatalog(
            self._db, library.name,
            this_url, [library],
            annotator=self.annotator, live=False,
        )
        return catalog_response(catalog)

    def render(self):
        response = Response(render_template_string(admin_template))
        return response

    @property
    def registration_document(self):
        """Serve a document that describes the registration process,
        notably the terms of service for that process.

        The terms of service are hosted elsewhere; we only know the
        URL of the page they're stored.
        """
        document = dict()

        # The terms of service may be encapsulated in a link to
        # a web page.
        terms_of_service_url = ConfigurationSetting.sitewide(
            self._db, Configuration.REGISTRATION_TERMS_OF_SERVICE_URL
        ).value
        type = "text/html"
        rel = "terms-of-service"
        if terms_of_service_url:
            OPDSCatalog.add_link_to_catalog(
                document, rel=rel, type=type,
                href=terms_of_service_url,
            )

        # And/or the terms of service may be described in
        # human-readable HTML, which we'll present as a data: link.
        terms_of_service_html = ConfigurationSetting.sitewide(
            self._db, Configuration.REGISTRATION_TERMS_OF_SERVICE_HTML
        ).value
        if terms_of_service_html:
            encoded = base64.b64encode(terms_of_service_html)
            terms_of_service_link = "data:%s;base64,%s" % (type, encoded)
            OPDSCatalog.add_link_to_catalog(
                document, rel=rel, type=type,
                href=terms_of_service_link
            )

        return document

    def catalog_response(self, document, status=200):
        """Serve an OPDS 2.0 catalog."""
        if not isinstance(document, (bytes, str)):
            document = json.dumps(document)
        headers = {"Content-Type": OPDS_CATALOG_REGISTRATION_MEDIA_TYPE}
        return Response(document, status, headers=headers)

    def register(self, do_get=HTTP.debuggable_get):
        if request.method == 'GET':
            return self.catalog_response(self.registration_document)

        auth_url = request.form.get("url")
        self.log.info("Got request to register %s", auth_url)

        if not auth_url:
            return NO_AUTH_URL

        integration_contact_uri = request.form.get("contact")
        integration_contact_email = integration_contact_uri
        shared_secret = None
        auth_header = request.headers.get('Authorization')

        if auth_header and isinstance(auth_header, str) and "bearer" in auth_header.lower():
            shared_secret = auth_header.split(' ', 1)[1]
            self.log.info("Incoming shared secret: %s...", shared_secret[:4])

        # If 'stage' is not provided, it means the client doesn't make the testing/production
        # distinction. We have to assume they want production -- otherwise they wouldn't
        # bother registering.

        library_stage = request.form.get("stage")
        self.log.info("Incoming stage: %s", library_stage)
        library_stage = library_stage or Library.PRODUCTION_STAGE

        # NOTE: This is commented out until we can say that registration requires providing
        # a contact email and expect every new library to be on a circulation manager that
        # can meet this requirement.
        #
        # integration_contact_email = self._required_email_address(
        #     integration_contact_uri,
        #     "Invalid or missing configuration contact email address"
        # )
        if isinstance(integration_contact_email, ProblemDetail):
            return integration_contact_email

        # Registration is a complex multi-step process. Start a subtransaction
        # so we can back out of the whole thing if any part of it fails.
        __transaction = self._db.begin_nested()

        library = None
        elevated_permissions = False

        if shared_secret:
            # Look up a library by the provided shared secret. This will let us handle the
            # case where the library has changed URLs (auth_url does not match library.authentication_url)
            # but the shared secret is the same.
            library = get_one(self._db, Library, shared_secret=shared_secret)

            if not library:
                __transaction.rollback()
                return AUTHENTICATION_FAILURE.detailed(_("Provided shared secret is invalid"))

            # This gives the requestor an elevated level of permissions.
            elevated_permissions = True
            library_is_new = False

            if library.authentication_url != auth_url:
                # The library's authentication URL has changed, e.g. moved from HTTP to HTTPS.
                # The registration includes a valid shared secret, so it's okay to modify the
                # corresponding database field.
                #
                # We want to do this before the registration, so that we request the new URL
                # instead of the old one.
                library.authentication_url = auth_url

        if not library:
            # Either this is a library at a known authentication URL or it's a brand new library.
            (library, library_is_new) = get_one_or_create(self._db, Library, authentication_url=auth_url)

        registrar = LibraryRegistrar(self._db, do_get=do_get)
        result = registrar.register(library, library_stage)

        if isinstance(result, ProblemDetail):
            __transaction.rollback()
            return result

        # At this point registration (or re-registration) has succeeded, so we won't be
        # rolling back the subtransaction that created the Library.
        __transaction.commit()
        auth_document, hyperlinks_to_create = result

        # Now that we've completed the registration process, we know the opds_url -- it's
        # the 'start' link found in the auth_document.
        #
        # Registration will fail if this link is missing or the URL doesn't work, so we
        # can assume this is valid.
        opds_url = auth_document.root['href']

        if library_is_new:
            # The library was just created, so it had no opds_url. Set it now.
            library.opds_url = opds_url

        # The registration process may have queued up a number of Hyperlinks that needed
        # to be created (taken from the library's authentication document), but we also need
        # to create a hyperlink for the integration contact provided with the registration
        # request itself.
        if integration_contact_email:
            hyperlinks_to_create.append((Hyperlink.INTEGRATION_CONTACT_REL, [integration_contact_email]))

        reset_shared_secret = False

        if elevated_permissions:
            # If you have elevated permissions you may ask for the shared secret to be reset.
            reset_shared_secret = request.form.get("reset_shared_secret", False)

            if library.opds_url != opds_url:
                # The library's OPDS URL has changed, e.g. moved from HTTP to HTTPS.
                # Since we have elevated permissions, it's okay to modify the corresponding
                # database field.
                library.opds_url = opds_url

        for rel, candidates in hyperlinks_to_create:
            hyperlink, is_modified = library.set_hyperlink(rel, *candidates)
            if is_modified:
                # We need to send an email to this email address about what just happened.
                # This is either so the receipient can confirm that the address works, or
                # to inform them a new library is using their address.
                try:
                    hyperlink.notify(self.emailer, self.app.url_for)
                except SMTPException as exc:
                    self.log.error("EMAIL_SEND_PROBLEM, SMTPException:", exc_info=exc)
                    # We were unable to send the email due to an SMTP error
                    return INTEGRATION_ERROR.detailed(
                        _("SMTP error while sending email to %(address)s",
                          address=hyperlink.resource.href)
                    )
                except CannotSendEmail as exc:
                    self.log.error("EMAIL_SEND_PROBLEM, CannotSendEmail:", exc_info=exc)
                    return UNABLE_TO_NOTIFY.detailed(
                        _("The Registry was unable to send a notification email.")
                    )

        # Create an OPDS 2 catalog containing all available
        # information about the library.
        catalog = OPDSCatalog.library_catalog(
            library, include_private_information=True,
            url_for=self.app.url_for
        )

        # Annotate the catalog with some information specific to
        # the transaction that's happening right now.
        public_key = auth_document.public_key

        if public_key and public_key.get("type") == "RSA":
            public_key = RSA.importKey(public_key.get("value"))
            encryptor = PKCS1_OAEP.new(public_key)

            if not library.short_name:
                def dupe_check(candidate):
                    return Library.for_short_name(self._db, candidate) is not None

                library.short_name = Library.random_short_name(dupe_check)

            generate_secret = bool((library.shared_secret is None) or reset_shared_secret)

            if generate_secret:
                library.shared_secret = random_string(24)

            encrypted_secret = encryptor.encrypt(library.shared_secret.encode("utf8"))

            catalog["metadata"]["short_name"] = library.short_name
            catalog["metadata"]["shared_secret"] = base64.b64encode(encrypted_secret)

        if library_is_new:
            status_code = 201
        else:
            status_code = 200

        return self.catalog_response(catalog, status_code)


class ValidationController(BaseController):
    """Validates Resources based on validation codes.

    The confirmation codes were sent out in emails to the addresses that
    need to be validated, or otherwise communicated to someone who needs
    to click on the link to this controller.
    """

    MESSAGE_TEMPLATE = "<html><head><title>%(message)s</title><body>%(message)s</body></html>"

    def html_response(self, status_code, message):
        """Return a human-readable message as a minimal HTML page.

        This controller is used by human beings, so HTML is better
        than Problem Detail Documents.
        """
        headers = {"Content-Type": "text/html"}
        page = self.MESSAGE_TEMPLATE % dict(message=message)
        return Response(page, status_code, headers=headers)

    def confirm(self, resource_id, secret):
        """Confirm a secret for a URI, or don't.

        :return: A Response containing a simple HTML document.
        """
        if not secret:
            return self.html_response(404, _("No confirmation code provided"))
        if not resource_id:
            return self.html_response(404, _("No resource ID provided"))
        validation = get_one(self._db, Validation, secret=secret)
        resource = get_one(self._db, Resource, id=resource_id)
        if not resource:
            return self.html_response(404, _("No such resource"))

        if not validation:
            # The secret is invalid. This might be because the secret
            # is wrong, or because the Resource has already been
            # validated.
            #
            # Let's eliminate the 'Resource has already been validated'
            # possibility and take care of the other case next.
            if resource and resource.validation and resource.validation.success:
                return self.html_response(200, _("This URI has already been validated."))

        if (not validation or not validation.resource or validation.resource.id != resource_id):
            # For whatever reason the resource ID and secret don't match.
            # A generic error that doesn't reveal information is appropriate
            # in all cases.
            error = _("Confirmation code %r not found") % secret
            return self.html_response(404, error)

        # At this point we know that the resource has not been
        # confirmed, and that the secret matches the resource. The
        # only other problem might be that the validation has expired.
        if not validation.active:
            error = _(
                "Confirmation code %r has expired. Re-register to get another code.") % secret
            return self.html_response(400, error)
        validation.mark_as_successful()

        resource = validation.resource
        message = _("You successfully confirmed %s.") % resource.href
        return self.html_response(200, message)


class CoverageController(BaseController):
    """Converts coverage area descriptions to GeoJSON documents
    so they can be visualized.
    """

    def geojson_response(self, document):
        if isinstance(document, dict):
            document = json.dumps(document)
        headers = {"Content-Type": "application/geo+json"}
        return Response(document, 200, headers=headers)

    def lookup(self):
        coverage = request.args.get('coverage')
        try:
            coverage = json.loads(coverage)
        except ValueError:
            pass
        places, unknown, ambiguous = AuthenticationDocument.parse_coverage(
            self._db, coverage
        )
        document = Place.to_geojson(self._db, *places)

        # Extend the GeoJSON with extra information about parts of the
        # coverage document we found ambiguous or couldn't associate
        # with a Place.
        if unknown:
            document['unknown'] = unknown
        if ambiguous:
            document['ambiguous'] = ambiguous
        return self.geojson_response(document)

    def _geojson_for_service_area(self, service_type):
        """Serve a GeoJSON document describing some subset of the active
        library's service areas.
        """
        areas = [
            x.place for x in request.library.service_areas if x.type == service_type]
        return self.geojson_response(Place.to_geojson(self._db, *areas))

    def eligibility_for_library(self):
        """Serve a GeoJSON document representing the eligibility area
        for a specific library.
        """
        return self._geojson_for_service_area(ServiceArea.ELIGIBILITY)

    def focus_for_library(self):
        """Serve a GeoJSON document representing the focus area
        for a specific library.
        """
        return self._geojson_for_service_area(ServiceArea.FOCUS)