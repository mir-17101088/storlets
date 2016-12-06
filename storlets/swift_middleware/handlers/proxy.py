# Copyright (c) 2010-2015 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from six.moves.urllib.parse import quote
try:
    # We need to import copy helper functions from copy middleware
    # since it is introduced to swift.
    from swift.common.middleware.copy import \
        _check_copy_from_header as check_copy_from_header, \
        _check_destination_header as check_destination_header
except ImportError:
    # This is required to keep compatibility with
    # swift < 2.8.0 which does not have COPY middleware.
    from swift.common.constraints import check_copy_from_header, \
        check_destination_header
from swift.common.swob import HTTPBadRequest, HTTPUnauthorized, \
    HTTPMethodNotAllowed, HTTPPreconditionFailed, HTTPForbidden
from swift.common.utils import config_true_value, public, FileLikeIter, \
    list_from_csv, split_path
from swift.common.middleware.acl import clean_acl
from swift.common.wsgi import make_subrequest
from swift.proxy.controllers.base import get_account_info
from storlets.swift_middleware.handlers.base import StorletBaseHandler, \
    NotStorletRequest, NotStorletExecution


CONDITIONAL_KEYS = ['IF_MATCH', 'IF_NONE_MATCH', 'IF_MODIFIED_SINCE',
                    'IF_UNMODIFIED_SINCE']

REFERER_PREFIX = 'storlets'


class StorletProxyHandler(StorletBaseHandler):
    def __init__(self, request, conf, gateway_conf, app, logger):
        super(StorletProxyHandler, self).__init__(
            request, conf, gateway_conf, app, logger)
        self.storlet_containers = [self.storlet_container,
                                   self.storlet_dependency]
        self.agent = 'ST'
        self.extra_sources = []

        # A very initial hook for blocking requests
        self._should_block(request)

        if not self.is_storlet_request:
            # This is not storlet-related request, so pass it
            raise NotStorletRequest()

        # In proxy server, storlet handler validate if storlet enabled
        # at the account, anyway
        account_meta = get_account_info(self.request.environ,
                                        self.app)['meta']
        storlets_enabled = account_meta.get('storlet-enabled',
                                            'False')
        if not config_true_value(storlets_enabled):
            self.logger.debug('Account disabled for storlets')
            raise HTTPBadRequest('Account disabled for storlets',
                                 request=self.request)

        if self.is_storlet_acl_update:
            self.acl_string = self._validate_acl_update(self.request)
        elif self.is_storlet_object_update:
            # TODO(takashi): We have to validate metadata in COPY case
            self._validate_registration(self.request)
            raise NotStorletExecution()
        elif self.is_storlet_execution:
            self._setup_gateway()
        else:
            raise NotStorletExecution()

    def _should_block(self, request):
        # Currently, we have only one reason to block
        # requests at such an early stage of the processing:
        # we block requests with referer that have the internal prefix
        # of:
        if not request.referer:
            return
        if REFERER_PREFIX in request.referer:
            raise HTTPForbidden('Referrer containing %s'
                                ' is not allowed' % REFERER_PREFIX)

    def _parse_vaco(self):
        return self.request.split_path(3, 4, rest_with_last=True)

    def is_proxy_runnable(self, resp):
        """
        Check if the storlet should be executed at proxy server

        :param resp: swob.Response instance
        :return: Whether we should execute the storlet at proxy
        """
        # SLO / proxy only case:
        # storlet to be invoked now at proxy side:
        runnable = any(
            [self.execute_on_proxy,
             self.execute_range_on_proxy,
             self.is_slo_response(resp)])
        return runnable

    @property
    def is_storlet_request(self):
        return (self.is_storlet_execution or self.is_storlet_object_update
                or self.is_storlet_acl_update)

    @property
    def is_storlet_object_update(self):
        return (self.container in self.storlet_containers and self.obj
                and self.request.method in ['PUT', 'POST'])

    @property
    def is_storlet_acl_update(self):
        return (self.request.method == 'POST' and not self.obj and
                'X-Storlet-Container-Read' in self.request.headers)

    @property
    def is_put_copy_request(self):
        return 'X-Copy-From' in self.request.headers

    def _parse_storlet_params(self, headers):
        """
        Parse storlet parameters from storlet/dependency object metadata

        :returns: dict of storlet parameters
        """
        params = dict()
        for key in headers:
            if key.startswith('X-Object-Meta-Storlet'):
                params[key[len('X-Object-Meta-Storlet-'):]] = headers[key]
        return params

    def _validate_registration(self, req):
        """
        Validate parameters about storlet/dependency object when registrating

        :params req: swob.Request instance
        :raises ValueError: If some parameters are wrong
        """
        params = self._parse_storlet_params(req.headers)
        try:
            if self.container == self.storlet_container:
                self.logger.debug('updating object in storlet container. '
                                  'Sanity check')
                self.gateway_class.validate_storlet_registration(
                    params, self.obj)
            else:
                self.logger.debug('updating object in storlet dependency. '
                                  'Sanity check')
                self.gateway_class.validate_dependency_registration(
                    params, self.obj)
        except ValueError as e:
            self.logger.exception('Bad parameter')
            raise HTTPBadRequest(e.message)

    def _build_acl_string(self, user, storlet):
        acl_string = '%s.%s_%s' % (REFERER_PREFIX, user, storlet)
        return acl_string

    def _validate_acl_update(self, req):
        """
        Validate the request has the necessary headers for a
        storlet ACL update

        :params req: swob.Request instance
        :return: the resulting acl string that hould be added
        :raises HTTPBadRequest: If a header is missing or mulformed
        """
        # Make sure we are not meddling with the storlet containers
        if self.container in self.storlet_containers:
            msg = 'storlet ACL update cannot be a storlet container'
            raise HTTPBadRequest(msg)

        # Make sure the expected headers are supplied
        user_name = req.headers.get("X-Storlet-Container-Read", None)
        storlet_name = req.headers.get("X-Storlet-Name", None)
        if not user_name or not storlet_name:
            msg = 'storlet ACL update request is missing a mandatory header'
            raise HTTPBadRequest(msg)

        # Make sure the resulting acl is valid
        acl_string = '.r:%s' % self._build_acl_string(user_name, storlet_name)
        try:
            clean_acl('X-Container-Read', acl_string)
        except ValueError as e:
            msg = ('storlet ACL update request has invalid values %s'
                   % e.message)
            raise HTTPBadRequest(msg)

        # Make sure the resulting acl permits a single entity
        if ',' in acl_string:
            msg = ('storlet ACL update request has '
                   'mulformed storlet or user name')
            raise HTTPBadRequest(msg)

        # The request is valid. Keep the ACL string
        return acl_string

    def verify_access_to_storlet(self):
        """
        Verify access to the storlet object

        :return: storlet parameters
        :raises HTTPUnauthorized: If it fails to verify access
        """
        sobj = self.request.headers.get('X-Run-Storlet')
        spath = '/'.join(['', self.api_version, self.account,
                          self.storlet_container, sobj])
        self.logger.debug('Verify access to %s' % spath)

        new_env = dict(self.request.environ)
        if 'HTTP_TRANSFER_ENCODING' in new_env.keys():
            del new_env['HTTP_TRANSFER_ENCODING']

        for key in CONDITIONAL_KEYS:
            env_key = 'HTTP_' + key
            if env_key in new_env.keys():
                del new_env[env_key]

        auth_token = self.request.headers.get('X-Auth-Token')
        storlet_req = make_subrequest(
            new_env, 'HEAD', spath,
            headers={'X-Auth-Token': auth_token},
            swift_source=self.agent)

        resp = storlet_req.get_response(self.app)
        if not resp.is_success:
            raise HTTPUnauthorized('Failed to verify access to the storlet',
                                   request=self.request)

        params = self._parse_storlet_params(resp.headers)
        for key in ['Content-Length', 'X-Timestamp']:
            params[key] = resp.headers[key]
        return params

    def handle_request(self):
        if hasattr(self, self.request.method):
            try:
                handler = getattr(self, self.request.method)
                getattr(handler, 'publicly_accessible')
            except AttributeError:
                # TODO(kota_): add allowed_method list to Allow header
                return HTTPMethodNotAllowed(request=self.request)
            return handler()
        else:
            raise HTTPMethodNotAllowed(request=self.request)

    def _call_gateway(self, resp):
        sreq = self._build_storlet_request(self.request, resp.headers,
                                           resp.app_iter)
        return self.gateway.invocation_flow(sreq, self.extra_sources)

    def augment_storlet_request(self, params):
        """
        Add to request the storlet parameters to be used in case the request
        is forwarded to the data node (GET case)

        :param params: paramegers to be augmented to request
        """
        for key, val in params.iteritems():
            self.request.headers['X-Storlet-' + key] = val

    def gather_extra_sources(self):
        # (kota_): I know this is a crazy hack to set the resp
        # dinamically so that this is a temprorary way to make sure
        # the capability, this aboslutely needs cleanup more genelic
        if 'X-Storlet-Extra-Resources' in self.request.headers:
            try:
                resources = list_from_csv(
                    self.request.headers['X-Storlet-Extra-Resources'])
                # resourece should be /container/object
                for resource in resources:
                    # sanity check, if it's invalid path ValueError
                    # will be raisen
                    swift_path = ['', self.api_version, self.account]
                    swift_path.extend(split_path(resource, 2, 2, True))
                    sub_req = make_subrequest(
                        self.request.environ,
                        'GET', '/'.join(swift_path),
                        agent=self.agent)
                    sub_resp = sub_req.get_response(self.app)
                    # TODO(kota_): make this in another green thread
                    # expicially, in parallel with primary GET

                    self.extra_sources.append(
                        self._build_storlet_request(
                            self.request, sub_resp.headers,
                            sub_resp.app_iter))
            except ValueError:
                raise HTTPBadRequest(
                    'X-Storlet-Extra-Resource must be a csv with'
                    '/container/object format')

    @public
    def GET(self):
        """
        GET handler on Proxy
        """
        if self.is_range_request:
            raise HTTPBadRequest('Storlet execution with range header is not'
                                 ' supported', request=self.request)

        params = self.verify_access_to_storlet()
        self.augment_storlet_request(params)

        # Range requests:
        # Range requests are not allowed with storlet invocation.
        # To run a storlet on a selected input range use the X-Storlet-Range
        # header.
        # If the range request is to be executed on the proxy we
        # create an HTTP Range request based on X-Storlet-Range
        # and let the request continue so that we get the required
        # range as input to the storlet that would get executed on
        # the proxy.
        if self.execute_range_on_proxy:
            self.request.headers['Range'] = \
                self.request.headers['X-Storlet-Range']

        original_resp = self.request.get_response(self.app)
        if original_resp.status_int == 403:
            # The user is unauthoried to read from the container.
            # It might be, however, that the user is permitted
            # to read given that the required storlet is executed.
            if not self.request.environ['HTTP_X_USER_NAME']:
                # The requester is not even an authenticated user.
                self.logger.info(('Storlet run request by an'
                                  ' authenticated user'))
                raise HTTPUnauthorized('User is not authorized')

            user_name = self.request.environ['HTTP_X_USER_NAME']
            storlet_name = self.request.headers['X-Run-Storlet']
            internal_referer = '//%s' % self._build_acl_string(user_name,
                                                               storlet_name)
            self.logger.info(('Got 403 for original GET %s request. '
                              'Trying with storlet internal referer %s' %
                              (self.request.path, internal_referer)))
            self.request.referrer = self.request.referer = internal_referer
            original_resp = self.request.get_response(self.app)

        if original_resp.is_success:
            # The get request may be a SLO object GET request.
            # Simplest solution would be to invoke a HEAD
            # for every GET request to test if we are in SLO case.
            # In order to save the HEAD overhead we implemented
            # a slightly more involved flow:
            # At proxy side, we augment request with Storlet stuff
            # and let the request flow.
            # At object side, we invoke the plain (non Storlet)
            # request and test if we are in SLO case.
            # and invoke Storlet only if non SLO case.
            # Back at proxy side, we test if test received
            # full object to detect if we are in SLO case,
            # and invoke Storlet only if in SLO case.
            if self.is_proxy_runnable(original_resp):
                self.gather_extra_sources()
                return self.apply_storlet(original_resp)
            else:
                # Non proxy GET case: Storlet was already invoked at
                # object side
                # TODO(kota_): Do we need to pop the Transfer-Encoding/
                #              Content-Length header from the resp?
                if 'Transfer-Encoding' in original_resp.headers:
                    original_resp.headers.pop('Transfer-Encoding')

                original_resp.headers['Content-Length'] = None
                return original_resp

        else:
            # In failure case, we need nothing to do, just return original
            # response
            return original_resp

    def _validate_copy_request(self):
        # We currently block copy from account
        unsupported_headers = ['X-Copy-From-Account',
                               'Destination-Account',
                               'X-Fresh-Metadata']

        for header in unsupported_headers:
            if header in self.request.headers:
                raise HTTPBadRequest(
                    'Storlet on copy with %s is not supported' %
                    header)

    def handle_put_copy_response(self, out_md, app_iter):
        self._remove_storlet_headers(self.request.headers)
        if 'CONTENT_LENGTH' in self.request.environ:
            self.request.environ.pop('CONTENT_LENGTH')
        self.request.headers['Transfer-Encoding'] = 'chunked'
        self._set_metadata_in_headers(self.request.headers, out_md)

        self.request.environ['wsgi.input'] = FileLikeIter(app_iter)
        return self.request.get_response(self.app)

    def _remove_storlet_headers(self, headers):
        for key in headers.keys():
            if (key.startswith('X-Storlet-') or
                    key.startswith('X-Object-Meta-Storlet') or
                    key == 'X-Run-Storlet'):
                headers.pop(key)

    def base_handle_copy_request(self, src_container, src_obj,
                                 dest_container, dest_object):
        """
        Unified path for:
        PUT verb with X-Copy-From and
        COPY verb with Destination
        """
        # Get an iterator over the source object
        source_path = '/%s/%s/%s/%s' % (self.api_version, self.account,
                                        src_container, src_obj)
        source_req = self.request.copy_get()
        source_req.headers.pop('X-Backend-Storage-Policy-Index', None)
        source_req.headers.pop('X-Run-Storlet', None)
        source_req.path_info = source_path
        source_req.headers['X-Newest'] = 'true'

        src_resp = source_req.get_response(self.app)
        sreq = self._build_storlet_request(self.request, src_resp.headers,
                                           src_resp.app_iter)
        self.gather_extra_sources()
        sresp = self.gateway.invocation_flow(sreq, self.extra_sources)

        resp = self.handle_put_copy_response(sresp.user_metadata,
                                             sresp.data_iter)
        acct, path = src_resp.environ['PATH_INFO'].split('/', 3)[2:4]
        resp.headers['X-Storlet-Generated-From-Account'] = quote(acct)
        resp.headers['X-Storlet-Generated-From'] = quote(path)
        if 'last-modified' in src_resp.headers:
            resp.headers['X-Storlet-Generated-From-Last-Modified'] = \
                src_resp.headers['last-modified']
        return resp

    @public
    def PUT(self):
        """
        PUT handler on Proxy
        """

        params = self.verify_access_to_storlet()
        self.augment_storlet_request(params)
        if self.is_put_copy_request:
            self._validate_copy_request()
            src_container, src_obj = check_copy_from_header(self.request)
            dest_container = self.container
            dest_object = self.obj
            self.request.headers.pop('X-Copy-From', None)
            return self.base_handle_copy_request(src_container, src_obj,
                                                 dest_container, dest_object)

        # TODO(takashi): chunk size should be configurable
        reader = self.request.environ['wsgi.input'].read
        body_iter = iter(lambda: reader(65536), '')
        sreq = self._build_storlet_request(
            self.request, self.request.headers, body_iter)

        sresp = self.gateway.invocation_flow(sreq)
        self._set_metadata_in_headers(self.request.headers,
                                      sresp.user_metadata)
        return self.handle_put_copy_response(sresp.user_metadata,
                                             sresp.data_iter)

    @public
    def COPY(self):
        """
        COPY handler on Proxy
        """
        if not self.request.headers.get('Destination'):
            return HTTPPreconditionFailed(request=self.request,
                                          body='Destination header required')

        params = self.verify_access_to_storlet()
        self.augment_storlet_request(params)
        self._validate_copy_request()
        dest_container, dest_object = check_destination_header(self.request)

        # re-write the existing request as a PUT instead of creating a new one
        # TODO(eranr): do we want a new sub_request or re-write existing one as
        # we do below. See proxy obj controller COPY.
        self.request.method = 'PUT'
        self.request.path_info = '/v1/%s/%s/%s' % \
                                 (self.account, dest_container, dest_object)
        self.request.headers['Content-Length'] = 0
        del self.request.headers['Destination']

        return self.base_handle_copy_request(self.container, self.obj,
                                             dest_container, dest_object)

    @public
    def POST(self):
        """
        POST handler on Proxy
        Deals with storlet ACL updates
        """

        # Get the current container's ACL
        # We perform a sub request rather than get_container_info
        # since get_container_info bypasses authorization, and we
        # prefer to be on the safe side.
        target = ['', self.api_version, self.account, self.container]
        sub_req = make_subrequest(self.request.environ,
                                  'HEAD', '/'.join(target),
                                  agent=self.agent)
        sub_resp = sub_req.get_response(self.app)
        if sub_resp.status_int != 204:
            self.logger.info("Failed to retreive container metadata")
            return HTTPUnauthorized(('Unauthorized to get or modify '
                                     'the container ACL'))

        # Add the requested ACL
        read_acl = sub_resp.headers.get("X-Container-Read", None)
        if read_acl:
            new_read_acl = ','.join([read_acl, self.acl_string])
        else:
            new_read_acl = self.acl_string

        self.request.headers['X-Container-Read'] = new_read_acl
        resp = self.request.get_response(self.app)
        self.logger.info("Got post response, %s" % resp.status)
        return resp
