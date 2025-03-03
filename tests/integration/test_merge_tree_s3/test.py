import logging
import time
import os

import pytest
from helpers.cluster import ClickHouseCluster
from helpers.utility import generate_values, replace_config, SafeThread

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))


@pytest.fixture(scope="module")
def cluster():
    try:
        cluster = ClickHouseCluster(__file__)
        cluster.add_instance(
            "node",
            main_configs=[
                "configs/config.xml",
                "configs/config.d/storage_conf.xml",
                "configs/config.d/bg_processing_pool_conf.xml",
            ],
            stay_alive=True,
            with_minio=True,
        )

        cluster.add_instance(
            "node_with_limited_disk",
            main_configs=[
                "configs/config.d/storage_conf.xml",
                "configs/config.d/bg_processing_pool_conf.xml",
            ],
            with_minio=True,
            tmpfs=[
                "/jbod1:size=2M",
            ],
        )

        logging.info("Starting cluster...")
        cluster.start()
        logging.info("Cluster started")
        run_s3_mocks(cluster)

        yield cluster
    finally:
        cluster.shutdown()


FILES_OVERHEAD = 1
FILES_OVERHEAD_PER_COLUMN = 2  # Data and mark files
FILES_OVERHEAD_PER_PART_WIDE = FILES_OVERHEAD_PER_COLUMN * 3 + 2 + 6 + 1
FILES_OVERHEAD_PER_PART_COMPACT = 10 + 1


def create_table(node, table_name, **additional_settings):
    settings = {
        "storage_policy": "s3",
        "old_parts_lifetime": 0,
        "index_granularity": 512,
    }
    settings.update(additional_settings)

    create_table_statement = f"""
        CREATE TABLE {table_name} (
            dt Date,
            id Int64,
            data String,
            INDEX min_max (id) TYPE minmax GRANULARITY 3
        ) ENGINE=MergeTree()
        PARTITION BY dt
        ORDER BY (dt, id)
        SETTINGS {",".join((k+"="+repr(v) for k, v in settings.items()))}"""

    node.query(f"DROP TABLE IF EXISTS {table_name}")
    node.query(create_table_statement)


def run_s3_mocks(cluster):
    logging.info("Starting s3 mocks")
    mocks = (
        ("unstable_proxy.py", "resolver", "8081"),
        ("no_delete_objects.py", "resolver", "8082"),
    )
    for mock_filename, container, port in mocks:
        container_id = cluster.get_container_id(container)
        current_dir = os.path.dirname(__file__)
        cluster.copy_file_to_container(
            container_id,
            os.path.join(current_dir, "s3_mocks", mock_filename),
            mock_filename,
        )
        cluster.exec_in_container(
            container_id, ["python", mock_filename, port], detach=True
        )

    # Wait for S3 mocks to start
    for mock_filename, container, port in mocks:
        num_attempts = 100
        for attempt in range(num_attempts):
            ping_response = cluster.exec_in_container(
                cluster.get_container_id(container),
                ["curl", "-s", f"http://localhost:{port}/"],
                nothrow=True,
            )
            if ping_response != "OK":
                if attempt == num_attempts - 1:
                    assert (
                        ping_response == "OK"
                    ), f'Expected "OK", but got "{ping_response}"'
                else:
                    time.sleep(1)
            else:
                logging.debug(
                    f"mock {mock_filename} ({port}) answered {ping_response} on attempt {attempt}"
                )
                break

    logging.info("S3 mocks started")


def wait_for_delete_s3_objects(cluster, expected, timeout=30):
    minio = cluster.minio_client
    while timeout > 0:
        if len(list(minio.list_objects(cluster.minio_bucket, "data/"))) == expected:
            return
        timeout -= 1
        time.sleep(1)
    assert len(list(minio.list_objects(cluster.minio_bucket, "data/"))) == expected


@pytest.fixture(autouse=True)
@pytest.mark.parametrize("node_name", ["node"])
def drop_table(cluster, node_name):
    yield
    node = cluster.instances[node_name]
    minio = cluster.minio_client

    node.query("DROP TABLE IF EXISTS s3_test NO DELAY")

    try:
        wait_for_delete_s3_objects(cluster, 0)
    finally:
        # Remove extra objects to prevent tests cascade failing
        for obj in list(minio.list_objects(cluster.minio_bucket, "data/")):
            minio.remove_object(cluster.minio_bucket, obj.object_name)


@pytest.mark.parametrize(
    "min_rows_for_wide_part,files_per_part,node_name",
    [
        (0, FILES_OVERHEAD_PER_PART_WIDE, "node"),
        (8192, FILES_OVERHEAD_PER_PART_COMPACT, "node"),
    ],
)
def test_simple_insert_select(
    cluster, min_rows_for_wide_part, files_per_part, node_name
):
    node = cluster.instances[node_name]
    create_table(node, "s3_test", min_rows_for_wide_part=min_rows_for_wide_part)
    minio = cluster.minio_client

    values1 = generate_values("2020-01-03", 4096)
    node.query("INSERT INTO s3_test VALUES {}".format(values1))
    assert node.query("SELECT * FROM s3_test order by dt, id FORMAT Values") == values1
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/")))
        == FILES_OVERHEAD + files_per_part
    )

    values2 = generate_values("2020-01-04", 4096)
    node.query("INSERT INTO s3_test VALUES {}".format(values2))
    assert (
        node.query("SELECT * FROM s3_test ORDER BY dt, id FORMAT Values")
        == values1 + "," + values2
    )
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/")))
        == FILES_OVERHEAD + files_per_part * 2
    )

    assert (
        node.query("SELECT count(*) FROM s3_test where id = 1 FORMAT Values") == "(2)"
    )


@pytest.mark.parametrize("merge_vertical,node_name", [(True, "node"), (False, "node")])
def test_insert_same_partition_and_merge(cluster, merge_vertical, node_name):
    settings = {}
    if merge_vertical:
        settings["vertical_merge_algorithm_min_rows_to_activate"] = 0
        settings["vertical_merge_algorithm_min_columns_to_activate"] = 0

    node = cluster.instances[node_name]
    create_table(node, "s3_test", **settings)
    minio = cluster.minio_client

    node.query("SYSTEM STOP MERGES s3_test")
    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-03", 1024))
    )
    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-03", 2048))
    )
    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-03", 4096))
    )
    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-03", 1024, -1))
    )
    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-03", 2048, -1))
    )
    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-03", 4096, -1))
    )
    assert node.query("SELECT sum(id) FROM s3_test FORMAT Values") == "(0)"
    assert (
        node.query("SELECT count(distinct(id)) FROM s3_test FORMAT Values") == "(8192)"
    )
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/")))
        == FILES_OVERHEAD_PER_PART_WIDE * 6 + FILES_OVERHEAD
    )

    node.query("SYSTEM START MERGES s3_test")

    # Wait for merges and old parts deletion
    for attempt in range(0, 10):
        parts_count = node.query(
            "SELECT COUNT(*) FROM system.parts WHERE table = 's3_test' and active = 1 FORMAT Values"
        )

        if parts_count == "(1)":
            break

        if attempt == 9:
            assert parts_count == "(1)"

        time.sleep(1)

    assert node.query("SELECT sum(id) FROM s3_test FORMAT Values") == "(0)"
    assert (
        node.query("SELECT count(distinct(id)) FROM s3_test FORMAT Values") == "(8192)"
    )
    wait_for_delete_s3_objects(
        cluster, FILES_OVERHEAD_PER_PART_WIDE + FILES_OVERHEAD, timeout=45
    )


@pytest.mark.parametrize("node_name", ["node"])
def test_alter_table_columns(cluster, node_name):
    node = cluster.instances[node_name]
    create_table(node, "s3_test")
    minio = cluster.minio_client

    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-03", 4096))
    )
    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-03", 4096, -1))
    )

    node.query("ALTER TABLE s3_test ADD COLUMN col1 UInt64 DEFAULT 1")
    # To ensure parts have merged
    node.query("OPTIMIZE TABLE s3_test")

    assert node.query("SELECT sum(col1) FROM s3_test FORMAT Values") == "(8192)"
    assert (
        node.query("SELECT sum(col1) FROM s3_test WHERE id > 0 FORMAT Values")
        == "(4096)"
    )
    wait_for_delete_s3_objects(
        cluster,
        FILES_OVERHEAD + FILES_OVERHEAD_PER_PART_WIDE + FILES_OVERHEAD_PER_COLUMN,
    )

    node.query(
        "ALTER TABLE s3_test MODIFY COLUMN col1 String", settings={"mutations_sync": 2}
    )

    assert node.query("SELECT distinct(col1) FROM s3_test FORMAT Values") == "('1')"
    # and file with mutation
    wait_for_delete_s3_objects(
        cluster,
        FILES_OVERHEAD + FILES_OVERHEAD_PER_PART_WIDE + FILES_OVERHEAD_PER_COLUMN + 1,
    )

    node.query("ALTER TABLE s3_test DROP COLUMN col1", settings={"mutations_sync": 2})

    # and 2 files with mutations
    wait_for_delete_s3_objects(
        cluster, FILES_OVERHEAD + FILES_OVERHEAD_PER_PART_WIDE + 2
    )


@pytest.mark.parametrize("node_name", ["node"])
def test_attach_detach_partition(cluster, node_name):
    node = cluster.instances[node_name]
    create_table(node, "s3_test")
    minio = cluster.minio_client

    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-03", 4096))
    )
    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-04", 4096))
    )
    assert node.query("SELECT count(*) FROM s3_test FORMAT Values") == "(8192)"
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/")))
        == FILES_OVERHEAD + FILES_OVERHEAD_PER_PART_WIDE * 2
    )

    node.query("ALTER TABLE s3_test DETACH PARTITION '2020-01-03'")
    assert node.query("SELECT count(*) FROM s3_test FORMAT Values") == "(4096)"
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/")))
        == FILES_OVERHEAD + FILES_OVERHEAD_PER_PART_WIDE * 2
    )

    node.query("ALTER TABLE s3_test ATTACH PARTITION '2020-01-03'")
    assert node.query("SELECT count(*) FROM s3_test FORMAT Values") == "(8192)"
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/")))
        == FILES_OVERHEAD + FILES_OVERHEAD_PER_PART_WIDE * 2
    )

    node.query("ALTER TABLE s3_test DROP PARTITION '2020-01-03'")
    assert node.query("SELECT count(*) FROM s3_test FORMAT Values") == "(4096)"
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/")))
        == FILES_OVERHEAD + FILES_OVERHEAD_PER_PART_WIDE
    )

    node.query("ALTER TABLE s3_test DETACH PARTITION '2020-01-04'")
    node.query(
        "ALTER TABLE s3_test DROP DETACHED PARTITION '2020-01-04'",
        settings={"allow_drop_detached": 1},
    )
    assert node.query("SELECT count(*) FROM s3_test FORMAT Values") == "(0)"
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/"))) == FILES_OVERHEAD
    )


@pytest.mark.parametrize("node_name", ["node"])
def test_move_partition_to_another_disk(cluster, node_name):
    node = cluster.instances[node_name]
    create_table(node, "s3_test")
    minio = cluster.minio_client

    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-03", 4096))
    )
    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-04", 4096))
    )
    assert node.query("SELECT count(*) FROM s3_test FORMAT Values") == "(8192)"
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/")))
        == FILES_OVERHEAD + FILES_OVERHEAD_PER_PART_WIDE * 2
    )

    node.query("ALTER TABLE s3_test MOVE PARTITION '2020-01-04' TO DISK 'hdd'")
    assert node.query("SELECT count(*) FROM s3_test FORMAT Values") == "(8192)"
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/")))
        == FILES_OVERHEAD + FILES_OVERHEAD_PER_PART_WIDE
    )

    node.query("ALTER TABLE s3_test MOVE PARTITION '2020-01-04' TO DISK 's3'")
    assert node.query("SELECT count(*) FROM s3_test FORMAT Values") == "(8192)"
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/")))
        == FILES_OVERHEAD + FILES_OVERHEAD_PER_PART_WIDE * 2
    )


@pytest.mark.parametrize("node_name", ["node"])
def test_table_manipulations(cluster, node_name):
    node = cluster.instances[node_name]
    create_table(node, "s3_test")
    minio = cluster.minio_client

    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-03", 4096))
    )
    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-04", 4096))
    )

    node.query("RENAME TABLE s3_test TO s3_renamed")
    assert node.query("SELECT count(*) FROM s3_renamed FORMAT Values") == "(8192)"
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/")))
        == FILES_OVERHEAD + FILES_OVERHEAD_PER_PART_WIDE * 2
    )
    node.query("RENAME TABLE s3_renamed TO s3_test")

    assert node.query("CHECK TABLE s3_test FORMAT Values") == "(1)"

    node.query("DETACH TABLE s3_test")
    node.query("ATTACH TABLE s3_test")
    assert node.query("SELECT count(*) FROM s3_test FORMAT Values") == "(8192)"
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/")))
        == FILES_OVERHEAD + FILES_OVERHEAD_PER_PART_WIDE * 2
    )

    node.query("TRUNCATE TABLE s3_test")
    assert node.query("SELECT count(*) FROM s3_test FORMAT Values") == "(0)"
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/"))) == FILES_OVERHEAD
    )


@pytest.mark.parametrize("node_name", ["node"])
def test_move_replace_partition_to_another_table(cluster, node_name):
    node = cluster.instances[node_name]
    create_table(node, "s3_test")
    minio = cluster.minio_client

    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-03", 4096))
    )
    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-04", 4096))
    )
    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-05", 4096, -1))
    )
    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-06", 4096, -1))
    )
    assert node.query("SELECT sum(id) FROM s3_test FORMAT Values") == "(0)"
    assert node.query("SELECT count(*) FROM s3_test FORMAT Values") == "(16384)"
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/")))
        == FILES_OVERHEAD + FILES_OVERHEAD_PER_PART_WIDE * 4
    )

    create_table(node, "s3_clone")

    node.query("ALTER TABLE s3_test MOVE PARTITION '2020-01-03' TO TABLE s3_clone")
    node.query("ALTER TABLE s3_test MOVE PARTITION '2020-01-05' TO TABLE s3_clone")
    assert node.query("SELECT sum(id) FROM s3_test FORMAT Values") == "(0)"
    assert node.query("SELECT count(*) FROM s3_test FORMAT Values") == "(8192)"
    assert node.query("SELECT sum(id) FROM s3_clone FORMAT Values") == "(0)"
    assert node.query("SELECT count(*) FROM s3_clone FORMAT Values") == "(8192)"
    # Number of objects in S3 should be unchanged.
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/")))
        == FILES_OVERHEAD * 2 + FILES_OVERHEAD_PER_PART_WIDE * 4
    )

    # Add new partitions to source table, but with different values and replace them from copied table.
    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-03", 4096, -1))
    )
    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-05", 4096))
    )
    assert node.query("SELECT sum(id) FROM s3_test FORMAT Values") == "(0)"
    assert node.query("SELECT count(*) FROM s3_test FORMAT Values") == "(16384)"
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/")))
        == FILES_OVERHEAD * 2 + FILES_OVERHEAD_PER_PART_WIDE * 6
    )

    node.query("ALTER TABLE s3_test REPLACE PARTITION '2020-01-03' FROM s3_clone")
    node.query("ALTER TABLE s3_test REPLACE PARTITION '2020-01-05' FROM s3_clone")
    assert node.query("SELECT sum(id) FROM s3_test FORMAT Values") == "(0)"
    assert node.query("SELECT count(*) FROM s3_test FORMAT Values") == "(16384)"
    assert node.query("SELECT sum(id) FROM s3_clone FORMAT Values") == "(0)"
    assert node.query("SELECT count(*) FROM s3_clone FORMAT Values") == "(8192)"

    # Wait for outdated partitions deletion.
    wait_for_delete_s3_objects(
        cluster, FILES_OVERHEAD * 2 + FILES_OVERHEAD_PER_PART_WIDE * 4
    )

    node.query("DROP TABLE s3_clone NO DELAY")
    assert node.query("SELECT sum(id) FROM s3_test FORMAT Values") == "(0)"
    assert node.query("SELECT count(*) FROM s3_test FORMAT Values") == "(16384)"
    # Data should remain in S3
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/")))
        == FILES_OVERHEAD + FILES_OVERHEAD_PER_PART_WIDE * 4
    )

    node.query("ALTER TABLE s3_test FREEZE")
    # Number S3 objects should be unchanged.
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/")))
        == FILES_OVERHEAD + FILES_OVERHEAD_PER_PART_WIDE * 4
    )

    node.query("DROP TABLE s3_test NO DELAY")
    # Backup data should remain in S3.

    wait_for_delete_s3_objects(cluster, FILES_OVERHEAD_PER_PART_WIDE * 4)

    for obj in list(minio.list_objects(cluster.minio_bucket, "data/")):
        minio.remove_object(cluster.minio_bucket, obj.object_name)


@pytest.mark.parametrize("node_name", ["node"])
def test_freeze_unfreeze(cluster, node_name):
    node = cluster.instances[node_name]
    create_table(node, "s3_test")
    minio = cluster.minio_client

    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-03", 4096))
    )
    node.query("ALTER TABLE s3_test FREEZE WITH NAME 'backup1'")
    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-04", 4096))
    )
    node.query("ALTER TABLE s3_test FREEZE WITH NAME 'backup2'")

    node.query("TRUNCATE TABLE s3_test")
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/")))
        == FILES_OVERHEAD + FILES_OVERHEAD_PER_PART_WIDE * 2
    )

    # Unfreeze single partition from backup1.
    node.query(
        "ALTER TABLE s3_test UNFREEZE PARTITION '2020-01-03' WITH NAME 'backup1'"
    )
    # Unfreeze all partitions from backup2.
    node.query("ALTER TABLE s3_test UNFREEZE WITH NAME 'backup2'")

    wait_for_delete_s3_objects(cluster, FILES_OVERHEAD)

    # Data should be removed from S3.
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/"))) == FILES_OVERHEAD
    )


@pytest.mark.parametrize("node_name", ["node"])
def test_freeze_system_unfreeze(cluster, node_name):
    node = cluster.instances[node_name]
    create_table(node, "s3_test")
    create_table(node, "s3_test_removed")
    minio = cluster.minio_client

    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-04", 4096))
    )
    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-04", 4096))
    )
    node.query("ALTER TABLE s3_test FREEZE WITH NAME 'backup3'")
    node.query("ALTER TABLE s3_test_removed FREEZE WITH NAME 'backup3'")

    node.query("TRUNCATE TABLE s3_test")
    node.query("DROP TABLE s3_test_removed NO DELAY")
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/")))
        == FILES_OVERHEAD + FILES_OVERHEAD_PER_PART_WIDE * 2
    )

    # Unfreeze all data from backup3.
    node.query("SYSTEM UNFREEZE WITH NAME 'backup3'")

    wait_for_delete_s3_objects(cluster, FILES_OVERHEAD)

    # Data should be removed from S3.
    assert (
        len(list(minio.list_objects(cluster.minio_bucket, "data/"))) == FILES_OVERHEAD
    )


@pytest.mark.parametrize("node_name", ["node"])
def test_s3_disk_apply_new_settings(cluster, node_name):
    node = cluster.instances[node_name]
    create_table(node, "s3_test")

    config_path = os.path.join(
        SCRIPT_DIR,
        "./{}/node/configs/config.d/storage_conf.xml".format(
            cluster.instances_dir_name
        ),
    )

    def get_s3_requests():
        node.query("SYSTEM FLUSH LOGS")
        return int(
            node.query(
                "SELECT value FROM system.events WHERE event='S3WriteRequestsCount'"
            )
        )

    s3_requests_before = get_s3_requests()
    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-03", 4096))
    )
    s3_requests_to_write_partition = get_s3_requests() - s3_requests_before

    # Force multi-part upload mode.
    replace_config(
        config_path,
        "<s3_max_single_part_upload_size>33554432</s3_max_single_part_upload_size>",
        "<s3_max_single_part_upload_size>0</s3_max_single_part_upload_size>",
    )

    node.query("SYSTEM RELOAD CONFIG")

    s3_requests_before = get_s3_requests()
    node.query(
        "INSERT INTO s3_test VALUES {}".format(generate_values("2020-01-04", 4096, -1))
    )

    # There should be 3 times more S3 requests because multi-part upload mode uses 3 requests to upload object.
    assert get_s3_requests() - s3_requests_before == s3_requests_to_write_partition * 3


@pytest.mark.parametrize("node_name", ["node"])
def test_s3_disk_restart_during_load(cluster, node_name):
    node = cluster.instances[node_name]
    create_table(node, "s3_test")

    node.query(
        "INSERT INTO s3_test VALUES {}".format(
            generate_values("2020-01-04", 1024 * 1024)
        )
    )
    node.query(
        "INSERT INTO s3_test VALUES {}".format(
            generate_values("2020-01-05", 1024 * 1024, -1)
        )
    )

    def read():
        for ii in range(0, 20):
            logging.info("Executing %d query", ii)
            assert node.query("SELECT sum(id) FROM s3_test FORMAT Values") == "(0)"
            logging.info("Query %d executed", ii)
            time.sleep(0.2)

    def restart_disk():
        for iii in range(0, 5):
            logging.info("Restarting disk, attempt %d", iii)
            node.query("SYSTEM RESTART DISK s3")
            logging.info("Disk restarted, attempt %d", iii)
            time.sleep(0.5)

    threads = []
    for i in range(0, 4):
        threads.append(SafeThread(target=read))

    threads.append(SafeThread(target=restart_disk))

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()


@pytest.mark.parametrize("node_name", ["node"])
def test_s3_no_delete_objects(cluster, node_name):
    node = cluster.instances[node_name]
    create_table(
        node, "s3_test_no_delete_objects", storage_policy="no_delete_objects_s3"
    )
    node.query("DROP TABLE s3_test_no_delete_objects SYNC")


@pytest.mark.parametrize("node_name", ["node"])
def test_s3_disk_reads_on_unstable_connection(cluster, node_name):
    node = cluster.instances[node_name]
    create_table(node, "s3_test", storage_policy="unstable_s3")
    node.query(
        "INSERT INTO s3_test SELECT today(), *, toString(*) FROM system.numbers LIMIT 9000000"
    )
    for i in range(30):
        print(f"Read sequence {i}")
        assert node.query("SELECT sum(id) FROM s3_test").splitlines() == [
            "40499995500000"
        ]


@pytest.mark.parametrize("node_name", ["node"])
def test_lazy_seek_optimization_for_async_read(cluster, node_name):
    node = cluster.instances[node_name]
    node.query("DROP TABLE IF EXISTS s3_test NO DELAY")
    node.query(
        "CREATE TABLE s3_test (key UInt32, value String) Engine=MergeTree() ORDER BY key SETTINGS storage_policy='s3';"
    )
    node.query(
        "INSERT INTO s3_test SELECT * FROM generateRandom('key UInt32, value String') LIMIT 10000000"
    )
    node.query("SELECT * FROM s3_test WHERE value LIKE '%abc%' ORDER BY value LIMIT 10")
    node.query("DROP TABLE IF EXISTS s3_test NO DELAY")
    minio = cluster.minio_client
    for obj in list(minio.list_objects(cluster.minio_bucket, "data/")):
        minio.remove_object(cluster.minio_bucket, obj.object_name)


@pytest.mark.parametrize("node_name", ["node_with_limited_disk"])
def test_cache_with_full_disk_space(cluster, node_name):
    node = cluster.instances[node_name]
    node.query("DROP TABLE IF EXISTS s3_test NO DELAY")
    node.query(
        "CREATE TABLE s3_test (key UInt32, value String) Engine=MergeTree() ORDER BY key SETTINGS storage_policy='s3_with_cache_and_jbod';"
    )
    node.query(
        "INSERT INTO s3_test SELECT * FROM generateRandom('key UInt32, value String') LIMIT 500000"
    )
    node.query(
        "SELECT * FROM s3_test WHERE value LIKE '%abc%' ORDER BY value FORMAT Null"
    )
    assert node.contains_in_log(
        "Insert into cache is skipped due to insufficient disk space"
    )
    node.query("DROP TABLE IF EXISTS s3_test NO DELAY")


@pytest.mark.parametrize("node_name", ["node"])
def test_store_cleanup_disk_s3(cluster, node_name):
    node = cluster.instances[node_name]
    node.query("DROP TABLE IF EXISTS s3_test SYNC")
    node.query(
        "CREATE TABLE s3_test UUID '00000000-1000-4000-8000-000000000001' (n UInt64) Engine=MergeTree() ORDER BY n SETTINGS storage_policy='s3';"
    )
    node.query("INSERT INTO s3_test SELECT 1")

    node.stop_clickhouse(kill=True)
    path_to_data = "/var/lib/clickhouse/"
    node.exec_in_container(["rm", f"{path_to_data}/metadata/default/s3_test.sql"])
    node.start_clickhouse()

    node.wait_for_log_line(
        "Removing unused directory", timeout=90, look_behind_lines=1000
    )
    node.wait_for_log_line("directories from store")
    node.query(
        "CREATE TABLE s3_test UUID '00000000-1000-4000-8000-000000000001' (n UInt64) Engine=MergeTree() ORDER BY n SETTINGS storage_policy='s3';"
    )
    node.query("INSERT INTO s3_test SELECT 1")


@pytest.mark.parametrize("node_name", ["node"])
def test_cache_setting_compatibility(cluster, node_name):
    node = cluster.instances[node_name]

    node.query("DROP TABLE IF EXISTS s3_test NO DELAY")

    node.query(
        "CREATE TABLE s3_test (key UInt32, value String) Engine=MergeTree() ORDER BY key SETTINGS storage_policy='s3_cache_r';"
    )
    node.query(
        "INSERT INTO s3_test SELECT * FROM generateRandom('key UInt32, value String') LIMIT 500"
    )

    result = node.query("SYSTEM DROP FILESYSTEM CACHE")

    result = node.query(
        "SELECT count() FROM system.filesystem_cache WHERE cache_path LIKE '%persistent'"
    )
    assert int(result) == 0

    node.query("SELECT * FROM s3_test")

    result = node.query(
        "SELECT count() FROM system.filesystem_cache WHERE cache_path LIKE '%persistent'"
    )
    assert int(result) > 0

    config_path = os.path.join(
        SCRIPT_DIR,
        f"./{cluster.instances_dir_name}/node/configs/config.d/storage_conf.xml",
    )

    replace_config(
        config_path,
        "<do_not_evict_index_and_mark_files>1</do_not_evict_index_and_mark_files>",
        "<do_not_evict_index_and_mark_files>0</do_not_evict_index_and_mark_files>",
    )

    result = node.query("DESCRIBE CACHE 's3_cache_r'")
    assert result.strip().endswith("1")

    node.restart_clickhouse()

    result = node.query("DESCRIBE CACHE 's3_cache_r'")
    assert result.strip().endswith("0")

    result = node.query(
        "SELECT count() FROM system.filesystem_cache WHERE cache_path LIKE '%persistent'"
    )
    assert int(result) > 0

    node.query("SELECT * FROM s3_test FORMAT Null")

    assert not node.contains_in_log("No such file or directory: Cache info:")

    replace_config(
        config_path,
        "<do_not_evict_index_and_mark_files>0</do_not_evict_index_and_mark_files>",
        "<do_not_evict_index_and_mark_files>1</do_not_evict_index_and_mark_files>",
    )

    result = node.query(
        "SELECT count() FROM system.filesystem_cache WHERE cache_path LIKE '%persistent'"
    )
    assert int(result) > 0

    node.restart_clickhouse()

    result = node.query("DESCRIBE CACHE 's3_cache_r'")
    assert result.strip().endswith("1")

    node.query("SELECT * FROM s3_test FORMAT Null")

    assert not node.contains_in_log("No such file or directory: Cache info:")
