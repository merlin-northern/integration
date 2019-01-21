# Copyright 2018 Northern.tech AS
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        https://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.
import pytest
import random
import time

from api.client import ApiClient
from common import mongo, clean_mongo
from infra.cli import CliUseradm, CliDeviceauth, CliTenantadm
import api.deviceauth as deviceauth_v1
import api.deviceauth_v2 as deviceauth_v2
import api.useradm as useradm
import api.tenantadm as tenantadm
import api.deployments as deployments
import api.inventory as inventory
import util.crypto
from common import User, Device, Authset, Tenant, \
        create_user, create_tenant, create_tenant_user, \
        create_random_authset, create_authset, \
        get_device_by_id_data, change_authset_status

@pytest.yield_fixture(scope='function')
def clean_migrated_mongo(clean_mongo):
    deviceauth_cli = CliDeviceauth()
    useradm_cli = CliUseradm()

    deviceauth_cli.migrate()
    useradm_cli.migrate()

    yield clean_mongo

@pytest.yield_fixture(scope='function')
def clean_migrated_mongo_mt(clean_mongo):
    deviceauth_cli = CliDeviceauth()
    useradm_cli = CliUseradm()
    for t in ['tenant1', 'tenant2']:
        deviceauth_cli.migrate(t)
        useradm_cli.migrate(t)

    yield clean_mongo

@pytest.yield_fixture(scope="function")
def user(clean_migrated_mongo):
    yield create_user('user-foo@acme.com', 'correcthorse')

@pytest.yield_fixture(scope="function")
def devices(clean_migrated_mongo, user):
    uc = ApiClient(useradm.URL_MGMT)

    r = uc.call('POST',
                useradm.URL_LOGIN,
                auth=(user.name, user.pwd))
    assert r.status_code == 200
    utoken = r.text

    devices = []

    for _ in range(5):
        aset = create_random_authset(utoken)
        dev = Device(aset.did, aset.id_data, aset.pubkey)
        devices.append(dev)

    yield devices

@pytest.yield_fixture(scope="function")
def tenants_users(clean_migrated_mongo_mt):
    cli = CliTenantadm()
    api = ApiClient(tenantadm.URL_INTERNAL)

    names = ['tenant1', 'tenant2']
    tenants=[]

    for n in names:
        tenants.append(create_tenant(n))

    for t in tenants:
        for i in range(2):
            user = create_tenant_user(i, t)
            t.users.append(user)

    yield tenants

@pytest.yield_fixture(scope="function")
def tenants_users_devices(clean_migrated_mongo_mt, tenants_users):
    uc = ApiClient(useradm.URL_MGMT)

    for t in tenants_users:
        user = t.users[0]
        r = uc.call('POST',
                    useradm.URL_LOGIN,
                    auth=(user.name, user.pwd))
        assert r.status_code == 200
        utoken = r.text

        for _ in range(5):
            aset = create_random_authset(utoken, t.tenant_token)
            dev = Device(aset.did, aset.id_data, aset.pubkey, t.tenant_token)
            t.devices.append(dev)

    yield tenants_users

class TestPreauthBase:
    def do_test_ok(self, user, tenant_token=''):
        useradmm = ApiClient(useradm.URL_MGMT)
        devauthm = ApiClient(deviceauth_v2.URL_MGMT)
        devauthd = ApiClient(deviceauth_v1.URL_DEVICES)

        # log in user
        r = useradmm.call('POST',
                          useradm.URL_LOGIN,
                          auth=(user.name, user.pwd))
        assert r.status_code == 200

        utoken = r.text

        # preauth device
        priv, pub = util.crypto.rsa_get_keypair()
        id_data = {'mac': 'pretenditsamac'}
        body = deviceauth_v2.preauth_req(
                    id_data,
                    pub)
        r = devauthm.with_auth(utoken).call('POST',
                                            deviceauth_v2.URL_DEVICES,
                                            body)
        assert r.status_code == 201

        # device appears in device list
        r = devauthm.with_auth(utoken).call('GET',
                                            deviceauth_v2.URL_DEVICES)
        assert r.status_code == 200
        api_devs = r.json()

        assert len(api_devs) == 1
        api_dev = api_devs[0]

        assert api_dev['status'] == 'preauthorized'
        assert api_dev['identity_data'] == id_data
        assert len(api_dev['auth_sets']) == 1
        aset = api_dev['auth_sets'][0]

        assert aset['identity_data'] == id_data
        assert util.crypto.rsa_compare_keys(aset['pubkey'], pub)
        assert aset['status'] == 'preauthorized'

        # actual device can obtain auth token
        body, sighdr = deviceauth_v1.auth_req(id_data,
                                              pub,
                                              priv,
                                              tenant_token)

        r = devauthd.call('POST',
                          deviceauth_v1.URL_AUTH_REQS,
                          body,
                          headers=sighdr)

        assert r.status_code == 200

        # device and authset changed status to 'accepted'
        r = devauthm.with_auth(utoken).call('GET',
                                            deviceauth_v2.URL_DEVICES,
                                            path_params={'id': api_dev['id']})

        api_devs = r.json()
        assert len(api_devs) == 1

        api_dev = api_devs[0]
        assert api_dev['status'] == 'accepted'
        assert len(api_dev['auth_sets']) == 1

        aset = api_dev['auth_sets'][0]
        assert aset['status'] == 'accepted'

    def do_test_fail_duplicate(self, user, devices):
        useradmm = ApiClient(useradm.URL_MGMT)
        devauthm = ApiClient(deviceauth_v2.URL_MGMT)

        # log in user
        r = useradmm.call('POST',
                          useradm.URL_LOGIN,
                          auth=(user.name, user.pwd))
        assert r.status_code == 200

        utoken = r.text

        # preauth duplicate device
        priv, pub = util.crypto.rsa_get_keypair()
        id_data = devices[0].id_data
        body = deviceauth_v2.preauth_req(
                    id_data,
                    pub)
        r = devauthm.with_auth(utoken).call('POST',
                                            deviceauth_v2.URL_DEVICES,
                                            body)
        assert r.status_code == 409

        # device list is unmodified
        r = devauthm.with_auth(utoken).call('GET',
                                            deviceauth_v2.URL_DEVICES)
        assert r.status_code == 200
        api_devs = r.json()

        assert len(api_devs) == len(devices)

        # existing device has no new auth sets
        existing = [d for d in api_devs if d['identity_data'] == id_data]
        assert len(existing) == 1
        existing = existing[0]

        assert len(existing['auth_sets']) == 1
        aset = existing['auth_sets'][0]
        assert util.crypto.rsa_compare_keys(aset['pubkey'], devices[0].pubkey)
        assert aset['status'] == 'pending'


class TestPreauth(TestPreauthBase):
    def test_ok(self, user):
        self.do_test_ok(user)

    def test_fail_duplicate(self, user, devices):
        self.do_test_fail_duplicate(user, devices)

    def test_fail_bad_request(self, user):
        useradmm = ApiClient(useradm.URL_MGMT)
        devauthm = ApiClient(deviceauth_v2.URL_MGMT)

        # log in user
        r = useradmm.call('POST',
                          useradm.URL_LOGIN,
                          auth=(user.name, user.pwd))
        assert r.status_code == 200

        utoken = r.text

        # id data not json
        priv, pub = util.crypto.rsa_get_keypair()
        id_data = '{\"mac\": \"foo\"}'
        body = deviceauth_v2.preauth_req(
                    id_data,
                    pub)
        r = devauthm.with_auth(utoken).call('POST',
                                            deviceauth_v2.URL_DEVICES,
                                            body)
        assert r.status_code == 400

        # not a valid key
        id_data = {'mac': 'foo'}
        body = deviceauth_v2.preauth_req(
                    id_data,
                    'not a public key')
        r = devauthm.with_auth(utoken).call('POST',
                                            deviceauth_v2.URL_DEVICES,
                                            body)
        assert r.status_code == 400

class TestPreauthMultitenant(TestPreauthBase):
    def test_ok(self, tenants_users):
        user = tenants_users[0].users[0]

        self.do_test_ok(user, tenants_users[0].tenant_token)

        # check other tenant's devices unmodified
        user1 = tenants_users[1].users[0]
        devs1 = tenants_users[1].devices
        self.verify_devices_unmodified(user1, devs1)

    def test_fail_duplicate(self, tenants_users_devices):
        user = tenants_users_devices[0].users[0]
        devices = tenants_users_devices[0].devices

        self.do_test_fail_duplicate(user, devices)

        # check other tenant's devices unmodified
        user1 = tenants_users_devices[1].users[0]
        devs1 = tenants_users_devices[1].devices
        self.verify_devices_unmodified(user1, devs1)

    def verify_devices_unmodified(self, user, in_devices):
        devauthm = ApiClient(deviceauth_v2.URL_MGMT)
        useradmm = ApiClient(useradm.URL_MGMT)

        r = useradmm.call('POST',
                          useradm.URL_LOGIN,
                          auth=(user.name, user.pwd))
        assert r.status_code == 200

        utoken = r.text

        r = devauthm.with_auth(utoken).call('GET',
                                            deviceauth_v2.URL_DEVICES)
        assert r.status_code == 200
        api_devs = r.json()

        assert len(api_devs) == len(in_devices)
        for ad in api_devs:
            assert ad['status'] == 'pending'

            orig_device = [d for d in in_devices if d.id_data == ad['identity_data']]
            assert len(orig_device) == 1
            orig_device = orig_device[0]

            assert len(ad['auth_sets']) == 1
            aset = ad['auth_sets'][0]
            assert util.crypto.rsa_compare_keys(aset['pubkey'], orig_device.pubkey)

def make_devs_with_authsets(user, tenant_token=''):
    """ create a good number of devices, some with >1 authsets, with different statuses.
        returns DevWithAuthsets objects."""
    useradmm = ApiClient(useradm.URL_MGMT)

    # log in user
    r = useradmm.call('POST',
                      useradm.URL_LOGIN,
                      auth=(user.name, user.pwd))
    assert r.status_code == 200

    utoken = r.text

    devices = []

    # some vanilla 'pending' devices, single authset
    for _ in range(5):
        dev = make_pending_device(utoken, 1, tenant_token=tenant_token)
        devices.append(dev)

    # some pending devices with > 1 authsets
    for i in range(2):
        dev = make_pending_device(utoken, 3, tenant_token=tenant_token)
        devices.append(dev)

    # some 'accepted' devices, single authset
    for _ in range(3):
        dev = make_accepted_device(utoken, 1, tenant_token=tenant_token)
        devices.append(dev)

    # some 'accepted' devices with >1 authsets
    for _ in range(2):
        dev = make_accepted_device(utoken, 3, tenant_token=tenant_token)
        devices.append(dev)

    # some rejected devices
    for _ in range(2):
        dev = make_rejected_device(utoken, 3, tenant_token=tenant_token)
        devices.append(dev)

    # preauth'd devices
    for i in range(2):
        dev = make_preauthd_device(utoken)
        devices.append(dev)

    # preauth'd devices with extra 'pending' sets
    for i in range(2):
        dev = make_preauthd_device_with_pending(utoken, num_pending=2, tenant_token=tenant_token)
        devices.append(dev)

    return devices

@pytest.yield_fixture(scope="function")
def devs_authsets(user):
    yield make_devs_with_authsets(user)

@pytest.yield_fixture(scope="function")
def tenants_devs_authsets(tenants_users):
    for t in tenants_users:
        devs = make_devs_with_authsets(t.users[0], t.tenant_token)
        t.devices = devs

    yield tenants_users

def rand_id_data():
    mac = ":".join(["{:02x}".format(random.randint(0x00, 0xFF), 'x') for i in range(6)])
    sn = "".join(["{}".format(random.randint(0x00, 0xFF)) for i in range(6)])

    return {'mac': mac, 'sn': sn}

def make_pending_device(utoken, num_auth_sets=1, tenant_token=''):
    id_data = rand_id_data()

    dev = None
    for i in range(num_auth_sets):
        priv, pub = util.crypto.rsa_get_keypair()
        new_set = create_authset(id_data, pub, priv, utoken, tenant_token=tenant_token)

        if dev is None:
            dev = Device(new_set.did, new_set.id_data, utoken, tenant_token)

        dev.authsets.append(new_set)

    dev.status = 'pending'

    return dev

def make_accepted_device(utoken, num_auth_sets=1, num_accepted=1, tenant_token=''):
    dev = make_pending_device(utoken, num_auth_sets, tenant_token=tenant_token)

    for i in range(num_accepted):
        aset_id = dev.authsets[i].id
        change_authset_status(dev.id, aset_id, 'accepted', utoken)

        dev.authsets[i].status = 'accepted'

    dev.status = 'accepted'

    return dev

def make_rejected_device(utoken, num_auth_sets=1, tenant_token=''):
    dev = make_pending_device(utoken, num_auth_sets, tenant_token=tenant_token)

    for i in range(num_auth_sets):
        aset_id = dev.authsets[i].id
        change_authset_status(dev.id, aset_id, 'rejected', utoken)

        dev.authsets[i].status = 'rejected'

    dev.status = 'rejected'

    return dev

def make_preauthd_device(utoken):
    devauthm = ApiClient(deviceauth_v2.URL_MGMT)

    priv, pub = util.crypto.rsa_get_keypair()
    id_data = rand_id_data()

    body = deviceauth_v2.preauth_req(
                id_data,
                pub)
    r = devauthm.with_auth(utoken).call('POST',
                                        deviceauth_v2.URL_DEVICES,
                                        body)
    assert r.status_code == 201

    api_dev = get_device_by_id_data(id_data, utoken)
    assert len(api_dev['auth_sets']) == 1
    aset = api_dev['auth_sets'][0]

    dev = Device(api_dev['id'], id_data, pub)
    dev.authsets.append(Authset(aset['id'], dev.id, id_data, pub, priv, 'preauthorized'))

    dev.status = 'preauthorized'

    return dev

def make_preauthd_device_with_pending(utoken, num_pending=1, tenant_token=''):
    dev = make_preauthd_device(utoken)

    for i in range(num_pending):
        priv, pub = util.crypto.rsa_get_keypair()
        aset = create_authset(dev.id_data, pub, priv, utoken, tenant_token=tenant_token)
        dev.authsets.append(Authset(aset.id, aset.did, dev.id_data, pub, priv, 'pending'))

    return dev


class TestDeviceMgmtBase:
    def do_test_ok_get_devices(self, devs_authsets, user):
        da = ApiClient(deviceauth_v2.URL_MGMT)
        ua = ApiClient(useradm.URL_MGMT)

        # log in user
        r = ua.call('POST',
                    useradm.URL_LOGIN,
                    auth=(user.name, user.pwd))
        assert r.status_code == 200

        utoken = r.text

        # test cases
        for status, page, per_page in [
                (None, None, None),
                ('pending', None, None),
                ('accepted', None, None),
                ('rejected', None, None),
                ('preauthorized', None, None),
                (None, 1, 10),
                (None, 3, 10),
                (None, 2, 5),
                ('accepted', 1, 4),
                ('accepted', 2, 4),
                ('accepted', 5, 2),
                ('pending', 2, 2)]:
            qs_params = {}

            if status is not None:
                qs_params['status'] = status
            if page is not None:
                qs_params['page'] = page
            if per_page is not None:
                qs_params['per_page'] = per_page

            r = da.with_auth(utoken).call('GET',
                                      deviceauth_v2.URL_DEVICES,
                                      qs_params=qs_params)
            assert r.status_code == 200
            api_devs = r.json()

            ref_devs = filter_and_page_devs(devs_authsets, page=page, per_page=per_page, status=status)

            self._compare_devs(ref_devs, api_devs)

    def do_test_get_device(self, devs_authsets, user):
        da = ApiClient(deviceauth_v2.URL_MGMT)
        ua = ApiClient(useradm.URL_MGMT)

        # log in user
        r = ua.call('POST',
                    useradm.URL_LOGIN,
                    auth=(user.name, user.pwd))
        assert r.status_code == 200

        utoken = r.text

        # existing devices
        for dev in devs_authsets:
            r = da.with_auth(utoken).call('GET',
                                      deviceauth_v2.URL_DEVICE,
                                      path_params={'id': dev.id})
            assert r.status_code == 200
            api_dev = r.json()

            self._compare_dev(dev, api_dev)

        # non-existent devices
        for id in ['foo', 'bar']:
            r = da.with_auth(utoken).call('GET',
                                      deviceauth_v2.URL_DEVICE,
                                      path_params={'id': id})
            assert r.status_code == 404

    def do_test_delete_device_ok(self, devs_authsets, user, tenant_token=''):
        devapim = ApiClient(deviceauth_v2.URL_MGMT)
        devapid = ApiClient(deviceauth_v1.URL_DEVICES)
        userapi = ApiClient(useradm.URL_MGMT)
        depapi = ApiClient(deployments.URL_DEVICES)

        # log in user
        r = userapi.call('POST',
                    useradm.URL_LOGIN,
                    auth=(user.name, user.pwd))
        assert r.status_code == 200

        utoken = r.text

        # decommission a pending device
        dev_pending = filter_and_page_devs(devs_authsets, status='pending')[0]
        r = devapim.with_auth(utoken).call('DELETE',
                                  deviceauth_v2.URL_DEVICE,
                                  path_params={'id': dev_pending.id})
        assert r.status_code == 204

        # only verify the device is gone
        r = devapim.with_auth(utoken).call('GET',
                                  deviceauth_v2.URL_DEVICE,
                                  path_params={'id': dev_pending.id})
        assert r.status_code == 404

        # log in an accepted device
        dev_acc = filter_and_page_devs(devs_authsets, status='accepted')[0]

        body, sighdr = deviceauth_v1.auth_req(dev_acc.id_data,
                                        dev_acc.authsets[0].pubkey,
                                        dev_acc.authsets[0].privkey,
                                        tenant_token)

        r = devapid.call('POST',
                         deviceauth_v1.URL_AUTH_REQS,
                         body,
                         headers=sighdr)
        assert r.status_code == 200
        dtoken = r.text

        # decommission the accepted device
        r = devapim.with_auth(utoken).call('DELETE',
                                   deviceauth_v2.URL_DEVICE,
                                   path_params={'id': dev_acc.id})
        assert r.status_code == 204

        # verify the device lost access
        r = depapi.with_auth(dtoken).call('GET',
                                   deployments.URL_NEXT,
                                   qs_params={'device_type': 'foo',
                                              'artifact_name': 'bar'})
        assert r.status_code == 401

        # verify the device is gone
        r = devapim.with_auth(utoken).call('GET',
                                   deviceauth_v2.URL_DEVICE,
                                   path_params={'id': dev_acc.id})
        assert r.status_code == 404

    def do_test_delete_device_not_found(self, devs_authsets, user):
        ua = ApiClient(useradm.URL_MGMT)
        da = ApiClient(deviceauth_v2.URL_MGMT)

        # log in user
        r = ua.call('POST',
                    useradm.URL_LOGIN,
                    auth=(user.name, user.pwd))
        assert r.status_code == 200

        utoken = r.text

        # try delete
        r = da.with_auth(utoken).call('DELETE',
                                   deviceauth_v2.URL_DEVICE,
                                   path_params={'id': 'foo'})
        assert r.status_code == 404

        # check device list unmodified
        r = da.with_auth(utoken).call('GET',
                                  deviceauth_v2.URL_DEVICES)

        assert r.status_code == 200
        api_devs = r.json()

        self._compare_devs(devs_authsets, api_devs)

    def do_test_device_count(self, devs_authsets, user):
        ua = ApiClient(useradm.URL_MGMT)
        da = ApiClient(deviceauth_v2.URL_MGMT)

        # log in user
        r = ua.call('POST',
                    useradm.URL_LOGIN,
                    auth=(user.name, user.pwd))
        assert r.status_code == 200

        utoken = r.text

        # test cases: successful counts
        for status in [None, \
                    'pending', \
                    'accepted', \
                    'rejected', \
                    'preauthorized']:
            qs_params={}
            if status is not None:
                qs_params={'status': status}

            r = da.with_auth(utoken).call('GET',
                                          deviceauth_v2.URL_DEVICES_COUNT,
                                          qs_params=qs_params)
            assert r.status_code == 200
            count = r.json()

            ref_devs = filter_and_page_devs(devs_authsets, status=status)

            ref_count = len(ref_devs)

            assert ref_count == count['count']

        # fail: bad request
        r = da.with_auth(utoken).call('GET',
                                      deviceauth_v2.URL_DEVICES_COUNT,
                                      qs_params={'status': 'foo'})
        assert r.status_code == 400

    def _compare_devs(self, devs, api_devs):
        assert len(api_devs) == len(devs)

        for i in range(len(api_devs)):
            self._compare_dev(devs[i], api_devs[i])

    def _compare_dev(self, dev, api_dev):
            assert api_dev['id'] == dev.id
            assert api_dev['identity_data'] == dev.id_data
            assert api_dev['status'] == dev.status

            assert len(api_dev['auth_sets']) == len(dev.authsets)

            # GOTCHA: don't rely on indexing, authsets can get reshuffled
            # depending on actual contents (we don't order them, so it's up to mongo)
            for api_aset in api_dev['auth_sets']:
                aset = [a for a in dev.authsets if util.crypto.rsa_compare_keys(a.pubkey, api_aset['pubkey'])]
                assert len(aset) == 1
                aset = aset[0]

                compare_aset(aset, api_aset)

    def _filter_and_page_devs(self, devs, page=None, per_page=None, status=None):
        if status is not None:
            devs = [d for d in devs if d.status==status]

        if page is None:
            page = 1

        if per_page is None:
            per_page = 20

        lo = (page-1)*per_page
        hi = lo + per_page

        return devs[lo:hi]

class TestDeviceMgmt(TestDeviceMgmtBase):
    def test_ok_get_devices(self, devs_authsets, user):
        self.do_test_ok_get_devices(devs_authsets, user)

    def test_get_device(self, devs_authsets, user):
        self.do_test_get_device(devs_authsets, user)

    def test_delete_device_ok(self, devs_authsets, user):
        self.do_test_delete_device_ok(devs_authsets, user)

    def test_delete_device_not_found(self, devs_authsets, user):
        self.do_test_delete_device_not_found(devs_authsets, user)

    def test_device_count(self, devs_authsets, user):
        self.do_test_device_count(devs_authsets, user)


class TestDeviceMgmtMultitenant(TestDeviceMgmtBase):
    def test_ok_get_devices(self, tenants_devs_authsets):
        for t in tenants_devs_authsets:
            self.do_test_ok_get_devices(t.devices, t.users[0])

    def test_get_device(self, tenants_devs_authsets):
        for t in tenants_devs_authsets:
            self.do_test_get_device(t.devices, t.users[0])

    def test_delete_device_ok(self, tenants_devs_authsets):
        for t in tenants_devs_authsets:
            self.do_test_delete_device_ok(t.devices, t.users[0], tenant_token=t.tenant_token)

    def test_delete_device_not_found(self, tenants_devs_authsets):
        for t in tenants_devs_authsets:
            self.do_test_delete_device_not_found(t.devices, t.users[0])

    def test_device_count(self, tenants_devs_authsets):
        for t in tenants_devs_authsets:
            self.do_test_device_count(t.devices, t.users[0])

    def test_limits_max_devices(self, tenants_devs_authsets):
        devauthi = ApiClient(deviceauth_v1.URL_INTERNAL)
        devauthm = ApiClient(deviceauth_v2.URL_MGMT)
        devauthd = ApiClient(deviceauth_v1.URL_DEVICES)
        useradmm = ApiClient(useradm.URL_MGMT)

        for t in tenants_devs_authsets:
            # get num currently accepted devices
            num_acc = len(filter_and_page_devs(t.devices, status='accepted'))

            # set limit to that
            r = devauthi.call('PUT',
                              deviceauth_v1.URL_LIMITS_MAX_DEVICES,
                              {'limit': num_acc},
                              path_params={'tid': t.id})
            assert r.status_code == 204

            # get limit via internal api
            r = devauthi.call('GET',
                              deviceauth_v1.URL_LIMITS_MAX_DEVICES,
                              path_params={'tid': t.id})
            assert r.status_code == 200

            assert r.json()['limit'] == num_acc

            # get limit via mgmt api
            r = useradmm.call('POST',
                              useradm.URL_LOGIN,
                              auth=(t.users[0].name, t.users[0].pwd))
            assert r.status_code == 200

            utoken = r.text

            r = devauthm.with_auth(utoken).call('GET',
                                        deviceauth_v2.URL_LIMITS_MAX_DEVICES)
            assert r.status_code == 200

            assert r.json()['limit'] == num_acc

            # try accept a device manually
            pending = filter_and_page_devs(t.devices, status='pending')[0]

            r = devauthm.with_auth(utoken).call('PUT',
                                           deviceauth_v2.URL_AUTHSET_STATUS,
                                           deviceauth_v2.req_status('accepted'),
                                           path_params={'did': pending.id, 'aid': pending.authsets[0].id })
            assert r.status_code == 422

            # try exceed the limit via preauth'd device
            preauthd = filter_and_page_devs(t.devices, status='preauthorized')[0]

            body, sighdr = deviceauth_v1.auth_req(preauthd.id_data,
                                                  preauthd.authsets[0].pubkey,
                                                  preauthd.authsets[0].privkey,
                                                  t.tenant_token)

            r = devauthd.call('POST',
                              deviceauth_v1.URL_AUTH_REQS,
                              body,
                              headers=sighdr)
            assert r.status_code == 401

class TestAuthsetMgmtBase:
    def do_test_get_authset_status(self, devs_authsets, user):
        devauthm = ApiClient(deviceauth_v2.URL_MGMT)
        useradmm = ApiClient(useradm.URL_MGMT)

        # log in user
        r = useradmm.call('POST',
                          useradm.URL_LOGIN,
                          auth=(user.name, user.pwd))
        assert r.status_code == 200

        utoken = r.text

        # try valid authsets
        for d in devs_authsets:
            for a in d.authsets:
                r = devauthm.with_auth(utoken).call('GET',
                                                    deviceauth_v2.URL_AUTHSET_STATUS,
                                                    path_params={'did': d.id, 'aid': a.id })
                assert r.status_code == 200
                assert r.json()['status'] == a.status

        # invalid authset or device
        for did, aid in [(devs_authsets[0].id, "foo"),
                         ("foo", "bar")]:
            r = devauthm.with_auth(utoken).call('GET',
                                                deviceauth_v2.URL_AUTHSET_STATUS,
                                                path_params={'did': did, 'aid': aid })
            assert r.status_code == 404

    def do_test_put_status_accept(self, devs_authsets, user, tenant_token=''):
        devauthm = ApiClient(deviceauth_v2.URL_MGMT)
        devauthd = ApiClient(deviceauth_v1.URL_DEVICES)
        useradmm = ApiClient(useradm.URL_MGMT)
        deploymentsd = ApiClient(deployments.URL_DEVICES)

        # log in user
        r = useradmm.call('POST',
                          useradm.URL_LOGIN,
                          auth=(user.name, user.pwd))
        assert r.status_code == 200
        utoken = r.text

        # select interesting devices - pending, rejected, or accepted/preauthd with extra authsets
        devs = []
        for status in ['pending',
                       'rejected',
                       'accepted',
                       'preauthorized']:
            found = filter_and_page_devs(devs_authsets, status=status)
            if status == 'accepted' or status == 'preauthorized':
                found = [d for d in found if len(d.authsets) > 1]

            devs.extend(found)

        # test acceptance for various kinds of devs
        for dev in devs:
            # for accepted devs - first actually get a device token
            dtoken = None
            if dev.status == 'accepted':
                accepted = [a for a in dev.authsets if a.status == 'accepted'][0]
                body, sighdr = deviceauth_v1.auth_req(accepted.id_data,
                                                      accepted.pubkey,
                                                      accepted.privkey,
                                                      tenant_token)

                r = devauthd.call('POST',
                                  deviceauth_v1.URL_AUTH_REQS,
                                  body,
                                  headers=sighdr)

                assert r.status_code == 200
                dtoken = r.text

            # find some pending or rejected authset
            aset = [a for a in dev.authsets if a.status == 'pending' or a.status == 'rejected'][0]

            # accept the authset
            change_authset_status(dev.id, aset.id, 'accepted', utoken)

            # in case of originally preauthd/accepted devs: the original authset must be rejected now
            if dev.status in ['accepted', 'preauthorized']:
                aset_to_reject = [a for a in dev.authsets if a.status == dev.status]
                assert len(aset_to_reject) == 1
                aset_to_reject[0].status = 'rejected'

            # in all cases, device is now 'accepted', along with the just accepted authset
            dev.status = 'accepted'
            aset.status = 'accepted'

            # verify device is correct in the api
            self.verify_dev_after_status_update(dev, utoken)

            # if the device used to be accepted - check it lost access
            if dtoken is not None:
                r = deploymentsd.with_auth(dtoken).call('GET',
                                                        deployments.URL_NEXT,
                                                        qs_params={'device_type': 'foo',
                                                                   'artifact_name': 'bar'})
                assert r.status_code == 401

            # device should also be provisioned in inventory
            time.sleep(1)
            self.verify_dev_provisioned(dev, utoken)

    def do_test_put_status_reject(self, devs_authsets, user, tenant_token=''):
        devauthm = ApiClient(deviceauth_v2.URL_MGMT)
        devauthd = ApiClient(deviceauth_v1.URL_DEVICES)
        useradmm = ApiClient(useradm.URL_MGMT)
        deploymentsd = ApiClient(deployments.URL_DEVICES)

        # log in user
        r = useradmm.call('POST',
                          useradm.URL_LOGIN,
                          auth=(user.name, user.pwd))
        assert r.status_code == 200
        utoken = r.text

        devs = []
        for status in ['pending',
                       'accepted',
                       'preauthorized']:
            found = filter_and_page_devs(devs_authsets, status=status)
            devs.extend(found)

        for dev in devs:
            aset = None
            dtoken = None

            # for accepted or preauthd devs, reject the accepted/preauthd set
            # otherwise just select something
            if dev.status in ['accepted', 'preauthorized']:
                aset = [a for a in dev.authsets if a.status == dev.status]
                assert len(aset) == 1
                aset = aset[0]
            else:
                aset = dev.authsets[0]

            # for accepted devs, also have an active device and check it loses api access
            if dev.status == 'accepted':
                body, sighdr = deviceauth_v1.auth_req(aset.id_data,
                                                      aset.pubkey,
                                                      aset.privkey,
                                                      tenant_token)

                r = devauthd.call('POST',
                                  deviceauth_v1.URL_AUTH_REQS,
                                  body,
                                  headers=sighdr)

                assert r.status_code == 200
                dtoken = r.text

            # reject the authset
            change_authset_status(dev.id, aset.id, 'rejected', utoken)

            # the given authset always changes to 'rejected'
            aset.status='rejected'

            # if all other asets are also rejected, the device becomes too
            # otherwise it's 'pending'
            rej_asets = [a for a in dev.authsets if a.id != aset.id and a.status == 'rejected']

            if len(rej_asets) == len(dev.authsets) - 1:
                dev.status = 'rejected'
            else:
                dev.status = 'pending'

            # check if the api device is consistent
            self.verify_dev_after_status_update(dev, utoken)

            # if we rejected an accepted, active device, check that it lost access
            if dtoken is not None:
                r = deploymentsd.with_auth(dtoken).call('GET',
                                           deployments.URL_NEXT,
                                           qs_params={'device_type': 'foo',
                                                      'artifact_name': 'bar'})
                assert r.status_code == 401

    def do_test_put_status_failed(self, devs_authsets, user):
        useradmm = ApiClient(useradm.URL_MGMT)
        devauthm = ApiClient(deviceauth_v2.URL_MGMT)

        r = useradmm.call('POST',
                          useradm.URL_LOGIN,
                          auth=(user.name, user.pwd))
        assert r.status_code == 200
        utoken = r.text

        # not found: valid device, bogus authset
        r = devauthm.with_auth(utoken).call('PUT',
                                       deviceauth_v2.URL_AUTHSET_STATUS,
                                       deviceauth_v2.req_status('accepted'),
                                       path_params={'did': devs_authsets[0].id, 'aid': "foo" })
        assert r.status_code == 404

        # not found: bogus device
        r = devauthm.with_auth(utoken).call('PUT',
                                       deviceauth_v2.URL_AUTHSET_STATUS,
                                       deviceauth_v2.req_status('accepted'),
                                       path_params={'did': "foo", 'aid': "bar" })
        assert r.status_code == 404

        # bad request - invalid status
        r = devauthm.with_auth(utoken).call('PUT',
                                       deviceauth_v2.URL_AUTHSET_STATUS,
                                       deviceauth_v2.req_status('invalid'),
                                       path_params={'did': devs_authsets[0].id, 'aid':  devs_authsets[0].authsets[0].id})
        assert r.status_code == 400

        # bad request - invalid payload
        r = devauthm.with_auth(utoken).call('PUT',
                                       deviceauth_v2.URL_AUTHSET_STATUS,
                                       '{"foo": "bar"}',
                                       path_params={'did': devs_authsets[0].id, 'aid':  devs_authsets[0].authsets[0].id})
        assert r.status_code == 400

    def do_test_delete_status(self, devs_authsets, user, tenant_token=''):
        devauthm = ApiClient(deviceauth_v2.URL_MGMT)
        devauthd = ApiClient(deviceauth_v1.URL_DEVICES)
        useradmm = ApiClient(useradm.URL_MGMT)
        deploymentsd = ApiClient(deployments.URL_DEVICES)

        # log in user
        r = useradmm.call('POST',
                          useradm.URL_LOGIN,
                          auth=(user.name, user.pwd))
        assert r.status_code == 200
        utoken = r.text

        for dev in devs_authsets:
            aset = None
            dtoken = None

            # for accepted or preauthd devs, reject the accepted/preauthd set
            # otherwise just select something
            if dev.status in ['accepted', 'preauthorized']:
                aset = [a for a in dev.authsets if a.status == dev.status]
                assert len(aset) == 1
                aset = aset[0]
            else:
                aset = dev.authsets[0]

            # for accepted devs, also have an active device and check it loses api access
            if dev.status == 'accepted':
                body, sighdr = deviceauth_v1.auth_req(aset.id_data,
                                                      aset.pubkey,
                                                      aset.privkey,
                                                      tenant_token)

                r = devauthd.call('POST',
                                  deviceauth_v1.URL_AUTH_REQS,
                                  body,
                                  headers=sighdr)

                assert r.status_code == 200
                dtoken = r.text

            # delete authset
            r = devauthm.with_auth(utoken).call('DELETE',
                                           deviceauth_v2.URL_AUTHSET,
                                           path_params={'did': dev.id, 'aid': aset.id })
            assert r.status_code == 204

            # authset should be gone
            dev.authsets.remove(aset)

            # if it's the last authset of a preauth'd device - the device should be completely gone
            if dev.status == 'preauthorized' and len(dev.authsets) == 0:
                r = devauthm.with_auth(utoken).call('GET',
                                          deviceauth_v2.URL_DEVICE,
                                          path_params={'id': dev.id})
                assert r.status_code == 404
                return
            else:
                # in other cases the device remains
                dev.status = self.compute_dev_status(dev.authsets)

            # check api dev is consistent
            self.verify_dev_after_status_update(dev, utoken)

            # verify the device lost access, if we had one
            if dtoken is not None:
                r = deploymentsd.with_auth(dtoken).call('GET',
                                                   deployments.URL_NEXT,
                                                   qs_params={'device_type': 'foo',
                                                              'artifact_name': 'bar'})
                assert r.status_code == 401

    def do_test_delete_status_failed(self, devs_authsets, user):
        useradmm = ApiClient(useradm.URL_MGMT)
        devauthm = ApiClient(deviceauth_v2.URL_MGMT)

        r = useradmm.call('POST',
                          useradm.URL_LOGIN,
                          auth=(user.name, user.pwd))
        assert r.status_code == 200
        utoken = r.text

        # not found: valid device, bogus authset
        r = devauthm.with_auth(utoken).call('DELETE',
                                       deviceauth_v2.URL_AUTHSET,
                                       path_params={'did': devs_authsets[0].id, 'aid': "foo" })
        assert r.status_code == 404

        # not found: bogus device
        r = devauthm.with_auth(utoken).call('DELETE',
                                       deviceauth_v2.URL_AUTHSET,
                                       path_params={'did': "foo", 'aid': "bar" })
        assert r.status_code == 404

    def verify_dev_after_status_update(self, dev, utoken):
        devauthm = ApiClient(deviceauth_v2.URL_MGMT)

        r = devauthm.with_auth(utoken).call('GET',
                                      deviceauth_v2.URL_DEVICE,
                                      path_params={'id': dev.id})
        assert r.status_code == 200
        api_dev = r.json()

        assert api_dev['status'] == dev.status
        assert len(api_dev['auth_sets']) == len(dev.authsets)

        for api_aset in api_dev['auth_sets']:
            aset = [a for a in dev.authsets if a.id == api_aset['id']]
            assert len(aset) == 1
            aset = aset[0]

            compare_aset(aset, api_aset)

    def compute_dev_status(self, authsets):
        accepted = [a for a in authsets if a.status == 'accepted']

        if len(accepted) > 0:
            return 'accepted'

        preauthd = [a for a in authsets if a.status == 'preauthorized']
        if len(preauthd) > 0:
            return 'preauthorized'

        pending =  [a for a in authsets if a.status == 'pending']
        if len(pending) > 0:
            return 'pending'

        # either the dev is actually 'rejected', or has no auth sets
        return 'rejected'

    def verify_dev_provisioned(self, dev, utoken):
        invm = ApiClient(inventory.URL_MGMT)

        r = invm.with_auth(utoken).call('GET',
                                        inventory.URL_DEVICE,
                                        path_params={'id': dev.id})
        assert r.status_code == 200

        api_dev = r.json()

class TestAuthsetMgmt(TestAuthsetMgmtBase):
    def test_get_authset_status(self, devs_authsets, user):
        self.do_test_get_authset_status(devs_authsets, user)

    def test_put_status_accept(self, devs_authsets, user):
        self.do_test_put_status_accept(devs_authsets, user)

    def test_put_status_reject(self, devs_authsets, user):
        self.do_test_put_status_reject(devs_authsets, user)

    def test_put_status_failed(self, devs_authsets, user):
        self.do_test_put_status_failed(devs_authsets, user)

    def test_delete_status(self, devs_authsets, user):
        self.do_test_delete_status(devs_authsets, user)

    def test_delete_status_failed(self, devs_authsets, user):
        self.do_test_delete_status_failed(devs_authsets, user)


class TestAuthsetMgmtMultitenant(TestAuthsetMgmtBase):
    def test_get_authset_status(self, tenants_devs_authsets):
        for t in tenants_devs_authsets:
            self.do_test_get_authset_status(t.devices, t.users[0])

    def test_put_status_accept(self, tenants_devs_authsets):
        for t in tenants_devs_authsets:
            self.do_test_put_status_accept(t.devices, t.users[0], t.tenant_token)

    def test_put_status_reject(self, tenants_devs_authsets):
        for t in tenants_devs_authsets:
            self.do_test_put_status_reject(t.devices, t.users[0], t.tenant_token)

    def test_put_status_failed(self, tenants_devs_authsets):
        for t in tenants_devs_authsets:
            self.do_test_put_status_failed(t.devices, t.users[0])

    def test_delete_status(self, tenants_devs_authsets):
        for t in tenants_devs_authsets:
            self.do_test_delete_status(t.devices, t.users[0], t.tenant_token)

    def test_delete_status_failed(self, tenants_devs_authsets):
        for t in tenants_devs_authsets:
            self.do_test_delete_status_failed(t.devices, t.users[0])

def filter_and_page_devs(devs, page=None, per_page=None, status=None):
        if status is not None:
            devs = [d for d in devs if d.status==status]

        if page is None:
            page = 1

        if per_page is None:
            per_page = 20

        lo = (page-1)*per_page
        hi = lo + per_page

        return devs[lo:hi]

def compare_aset(authset, api_authset):
       assert authset.id == api_authset['id']
       assert authset.id_data == api_authset['identity_data']
       assert util.crypto.rsa_compare_keys(authset.pubkey, api_authset['pubkey'])
       assert authset.status == api_authset['status']
