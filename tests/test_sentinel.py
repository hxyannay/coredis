from __future__ import annotations

import pytest

import coredis
from coredis.exceptions import (
    ConnectionError,
    PrimaryNotFoundError,
    ReadOnlyError,
    ReplicaNotFoundError,
    ReplicationError,
    ResponseError,
    TimeoutError,
)
from coredis.sentinel import Sentinel, SentinelConnectionPool
from tests.conftest import targets

pytestmarks = pytest.mark.asyncio


class SentinelTestClient:
    def __init__(self, cluster, id):
        self.cluster = cluster
        self.id = id

    async def sentinel_masters(self):
        self.cluster.connection_error_if_down(self)
        self.cluster.timeout_if_down(self)

        return {self.cluster.service_name: self.cluster.primary}

    async def sentinel_replicas(self, primary_name):
        self.cluster.connection_error_if_down(self)
        self.cluster.timeout_if_down(self)

        if primary_name != self.cluster.service_name:
            return []

        return self.cluster.replicas


class SentinelTestCluster:
    def __init__(self, service_name="mymaster", ip="127.0.0.1", port=6379):
        self.clients = {}
        self.primary = {
            "ip": ip,
            "port": port,
            "is_master": True,
            "is_sdown": False,
            "is_odown": False,
            "num-other-sentinels": 0,
        }
        self.service_name = service_name
        self.replicas = []
        self.nodes_down = set()
        self.nodes_timeout = set()

    def connection_error_if_down(self, node):
        if node.id in self.nodes_down:
            raise ConnectionError

    def timeout_if_down(self, node):
        if node.id in self.nodes_timeout:
            raise TimeoutError

    def client(self, host, port, **kwargs):
        return SentinelTestClient(self, (host, port))


@pytest.fixture()
def cluster(request):
    def teardown():
        coredis.sentinel.Redis = saved_Redis

    cluster = SentinelTestCluster()
    saved_Redis = coredis.sentinel.Redis
    coredis.sentinel.Redis = cluster.client
    request.addfinalizer(teardown)

    return cluster


@pytest.fixture()
def sentinel(request, cluster):
    return Sentinel([("foo", 26379), ("bar", 26379)])


async def test_discover_primary(sentinel):
    address = await sentinel.discover_primary("mymaster")
    assert address == ("127.0.0.1", 6379)


async def test_discover_primary_error(sentinel):
    with pytest.raises(PrimaryNotFoundError):
        await sentinel.discover_primary("xxx")


async def test_discover_primary_sentinel_down(cluster, sentinel):
    # Put first sentinel 'foo' down
    cluster.nodes_down.add(("foo", 26379))
    address = await sentinel.discover_primary("mymaster")
    assert address == ("127.0.0.1", 6379)
    # 'bar' is now first sentinel
    assert sentinel.sentinels[0].id == ("bar", 26379)


async def test_discover_primary_sentinel_timeout(cluster, sentinel):
    # Put first sentinel 'foo' down
    cluster.nodes_timeout.add(("foo", 26379))
    address = await sentinel.discover_primary("mymaster")
    assert address == ("127.0.0.1", 6379)
    # 'bar' is now first sentinel
    assert sentinel.sentinels[0].id == ("bar", 26379)


async def test_master_min_other_sentinels(cluster):
    sentinel = Sentinel([("foo", 26379)], min_other_sentinels=1)
    # min_other_sentinels
    with pytest.raises(PrimaryNotFoundError):
        await sentinel.discover_primary("mymaster")
    cluster.primary["num-other-sentinels"] = 2
    address = await sentinel.discover_primary("mymaster")
    assert address == ("127.0.0.1", 6379)


async def test_master_odown(cluster, sentinel):
    cluster.primary["is_odown"] = True
    with pytest.raises(PrimaryNotFoundError):
        await sentinel.discover_primary("mymaster")


async def test_master_sdown(cluster, sentinel):
    cluster.primary["is_sdown"] = True
    with pytest.raises(PrimaryNotFoundError):
        await sentinel.discover_primary("mymaster")


async def test_discover_replicas(cluster, sentinel):
    assert await sentinel.discover_replicas("mymaster") == []

    cluster.replicas = [
        {"ip": "replica0", "port": 1234, "is_odown": False, "is_sdown": False},
        {"ip": "replica1", "port": 1234, "is_odown": False, "is_sdown": False},
    ]
    assert await sentinel.discover_replicas("mymaster") == [
        ("replica0", 1234),
        ("replica1", 1234),
    ]

    # replica0 -> ODOWN
    cluster.replicas[0]["is_odown"] = True
    assert await sentinel.discover_replicas("mymaster") == [("replica1", 1234)]

    # replica1 -> SDOWN
    cluster.replicas[1]["is_sdown"] = True
    assert await sentinel.discover_replicas("mymaster") == []

    cluster.replicas[0]["is_odown"] = False
    cluster.replicas[1]["is_sdown"] = False

    # node0 -> DOWN
    cluster.nodes_down.add(("foo", 26379))
    assert await sentinel.discover_replicas("mymaster") == [
        ("replica0", 1234),
        ("replica1", 1234),
    ]
    cluster.nodes_down.clear()

    # node0 -> TIMEOUT
    cluster.nodes_timeout.add(("foo", 26379))
    assert await sentinel.discover_replicas("mymaster") == [
        ("replica0", 1234),
        ("replica1", 1234),
    ]


@targets("redis_sentinel", "redis_sentinel_resp2")
async def test_primary_for(client, host_ip):
    primary = client.primary_for("mymaster")
    assert await primary.ping()
    assert primary.connection_pool.primary_address == (host_ip, 6380)

    # Use internal connection check
    primary = client.primary_for("mymaster", check_connection=True)
    assert await primary.ping()


@targets("redis_sentinel", "redis_sentinel_resp2")
async def test_replica_for(client):
    replica = client.replica_for("mymaster")
    assert await replica.ping()


async def test_replica_for_slave_not_found_error(cluster, sentinel):
    cluster.primary["is_odown"] = True
    replica = sentinel.replica_for("mymaster", db=9)
    with pytest.raises(ReplicaNotFoundError):
        await replica.ping()


async def test_replica_round_robin(cluster, sentinel):
    cluster.replicas = [
        {"ip": "replica0", "port": 6379, "is_odown": False, "is_sdown": False},
        {"ip": "replica1", "port": 6379, "is_odown": False, "is_sdown": False},
    ]
    pool = SentinelConnectionPool("mymaster", sentinel)
    rotator = await pool.rotate_replicas()
    assert set(rotator) == {("replica0", 6379), ("replica1", 6379)}


async def test_autodecode(redis_sentinel_server):
    sentinel = Sentinel(sentinels=[redis_sentinel_server], decode_responses=True)
    assert await sentinel.primary_for("mymaster").ping() == "PONG"
    assert (
        await sentinel.primary_for("mymaster", decode_responses=False).ping() == b"PONG"
    )


@targets("redis_sentinel", "redis_sentinel_resp2")
async def test_ckquorum(client):
    assert await client.sentinels[0].sentinel_ckquorum("mymaster")


@pytest.mark.min_server_version("6.2.0")
@targets("redis_sentinel", "redis_sentinel_resp2")
async def test_sentinel_config_get(client):
    configs = await client.sentinels[0].sentinel_config_get("*")
    assert configs["resolve-hostnames"] == "yes"


@pytest.mark.min_server_version("6.2.0")
@targets("redis_sentinel", "redis_sentinel_resp2")
async def test_sentinel_config_set(client):
    await client.sentinels[0].sentinel_config_set("resolve-hostnames", "no")
    configs = await client.sentinels[0].sentinel_config_get("*")
    assert configs["resolve-hostnames"] == "no"


@targets("redis_sentinel", "redis_sentinel_resp2")
async def test_master_address_by_name(client):
    master_address = await client.sentinels[0].sentinel_get_master_addr_by_name(
        "mymaster"
    )
    assert master_address == await client.discover_primary("mymaster")


@targets("redis_sentinel", "redis_sentinel_resp2")
async def test_failover(client, mocker):
    mock_exec = mocker.patch.object(
        client.sentinels[0], "execute_command", autospec=True
    )
    mock_exec.return_value = True
    assert await client.sentinels[0].sentinel_failover("mymaster")
    assert mock_exec.call_args[0][1] == "mymaster"


@targets("redis_sentinel", "redis_sentinel_resp2")
async def test_flush_config(client):
    assert await client.sentinels[0].sentinel_flushconfig()


@targets("redis_sentinel", "redis_sentinel_resp2")
async def test_infocache(client):
    assert await client.sentinels[0].sentinel_flushconfig()
    info_cache = await client.sentinels[0].sentinel_infocache("mymaster")
    roles = {info["role"] for info in list(info_cache["mymaster"].values())}
    assert {"master", "slave"} & roles


@targets("redis_sentinel", "redis_sentinel_resp2")
async def test_sentinel_master(client):
    assert (await client.sentinels[0].sentinel_master("mymaster"))["is_master"]


@targets("redis_sentinel", "redis_sentinel_resp2")
async def test_sentinel_masters(client):
    assert (await client.sentinels[0].sentinel_masters())["mymaster"]["is_master"]


@targets("redis_sentinel", "redis_sentinel_resp2")
async def test_sentinel_replicas(client):
    assert not any(
        [
            k["is_master"]
            for k in (await client.sentinels[0].sentinel_replicas("mymaster"))
        ]
    )


@targets("redis_sentinel", "redis_sentinel_resp2")
async def test_no_replicas(client, mocker):
    p = await client.replica_for("mymaster")
    replica_rotate = mocker.patch.object(p.connection_pool, "rotate_replicas")
    replica_rotate.return_value = []
    with pytest.raises(ReplicaNotFoundError):
        await p.ping()


@targets("redis_sentinel", "redis_sentinel_resp2")
async def test_write_to_replica(client):
    p = await client.replica_for("mymaster")
    await p.ping()
    with pytest.raises(ReadOnlyError):
        await p.set("fubar", 1)


@targets("redis_sentinel", "redis_sentinel_resp2")
@pytest.mark.parametrize(
    "client_arguments", [({"cache": coredis.cache.TrackingCache()})]
)
async def test_sentinel_cache(client, client_arguments, mocker):
    await client.primary_for("mymaster").set("fubar", 1)

    assert await client.primary_for("mymaster").get("fubar") == "1"

    new_primary = client.primary_for("mymaster")
    new_replica = client.replica_for("mymaster")

    assert new_primary.cache
    assert new_replica.cache

    await new_primary.ping()
    await new_replica.ping()

    primary_spy = mocker.spy(new_primary, "execute_command")
    replica_spy = mocker.spy(new_replica, "execute_command")

    assert new_primary.cache.healthy
    assert new_replica.cache.healthy

    assert await new_primary.get("fubar") == "1"
    assert await new_replica.get("fubar") == "1"

    assert primary_spy.call_count == 1
    assert replica_spy.call_count == 0


@targets("redis_sentinel", "redis_sentinel_resp2")
async def test_replication(client):
    with client.primary_for("mymaster").ensure_replication(1, 500) as primary:
        await primary.set("fubar", 1)

    with pytest.raises(ReplicationError):
        with client.primary_for("mymaster").ensure_replication(2) as primary:
            await primary.set("fubar", 1)

    with pytest.raises(
        ResponseError, match="WAIT cannot be used with replica instances"
    ):
        with client.replica_for("mymaster").ensure_replication(2) as replica:
            await replica.set("fubar", 1)
