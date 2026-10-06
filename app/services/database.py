"""
SQLite persistence service.

Stores the results of a discovery run so the topology can be recalled
later, mirroring the CSV artifacts the original scripts produced:

    chasis.py        -> switch_summary / lldp_neighbors / arp_mappings
    Topology.py      -> devices / network_connections

Kept free of any PySide6/Qt imports like the other services so it can be
used and unit-tested without a GUI. Each call to save_run() replaces the
previous run's data in a single transaction (mirrors the original scripts'
overwrite-each-run behaviour).
"""

import math
from pathlib import Path

import pandas as pd
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine


def default_db_path():
    """Project root next to the app package: <project>/topology.db."""
    return Path(__file__).resolve().parent.parent.parent / "topology.db"


def _clean(value):
    """Convert a value into something SQLite can store. NaN floats (very
    common in pandas DataFrames) become None; everything else is passed
    through as-is."""
    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
    except TypeError:
        pass
    return value


class Database:
    def __init__(self, db_path=None, log_callback=None):
        """
        db_path: where the SQLite file lives. Defaults to <project>/topology.db
        log_callback(message: str) -> None, optional. Used instead of print().
        """
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.log_callback = log_callback or (lambda message: None)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._metadata = self._build_schema()
        self._engine = create_engine(f"sqlite:///{self.db_path}")
        self._metadata.create_all(self._engine)

    # ---------------------------------------------------------------
    # Schema
    # ---------------------------------------------------------------

    def _build_schema(self):
        metadata = MetaData()

        Table(
            "switch_summary",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("IP", String),
            Column("Status", String),
            Column("Local Chassis ID", String),
            Column("Local SysName", String),
            Column("Neighbors Found", Integer),
        )
        Table(
            "lldp_neighbors",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("Local IP", String),
            Column("Local SysName", String),
            Column("Neighbor Chassis ID", String),
            Column("Neighbor Management IP", String),
            Column("Neighbor Port", String),
            Column("Neighbor Port Desc", String),
            Column("Neighbor SysName", String),
        )
        Table(
            "arp_mappings",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("MAC", String),
            Column("IP", String),
        )
        Table(
            "devices",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("IP", String),
            Column("Status", String),
            Column("Local Chassis ID", String),
            Column("Local SysName", String),
        )
        Table(
            "network_connections",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("Local IP", String),
            Column("Local Port", String),
            Column("Neighbor IP", String),
            Column("Neighbor Port", String),
        )
        return metadata

    # ---------------------------------------------------------------
    # Writing
    # ---------------------------------------------------------------

    def save_run(self, summary_df, neighbors_df, arp_df, df_network, devices_df=None, accumulate=True):
        """Persists one full discovery run.

        switch_summary / lldp_neighbors / arp_mappings are replaced per run
        (fresh scan data). With accumulate=True (default) the resolved
        devices and network_connections are UNION-ed with earlier runs so a
        link seen once never disappears just because a later LLDP table
        aged it out — which keeps the topology stable on networks where
        neighbor records come and go between scans.

        Runs atomically — if anything fails, nothing changes."""
        summary = summary_df if summary_df is not None else pd.DataFrame()
        neighbors = neighbors_df if neighbors_df is not None else pd.DataFrame()
        arp = arp_df if arp_df is not None else pd.DataFrame()
        connections = df_network if df_network is not None else pd.DataFrame()
        devices = devices_df if devices_df is not None else pd.DataFrame()

        rows = {
            "switch_summary": self._rows_for(summary),
            "lldp_neighbors": self._rows_for(neighbors),
            "arp_mappings": self._rows_for(arp),
            "network_connections": self._rows_for(connections),
            "devices": self._rows_for(devices),
        }

        carried_links = 0
        if accumulate:
            existing_conns = self._read_table("network_connections")
            existing_devices = self._read_table("devices")
            merged_conns = self._merge_connections(existing_conns, connections)
            merged_devices = self._merge_devices(existing_devices, devices)
            carried_links = len(merged_conns) - len(self._rows_for(connections))
            rows["network_connections"] = self._rows_for(merged_conns)
            rows["devices"] = self._rows_for(merged_devices)

        with self._engine.begin() as conn:
            for table_name, table in self._metadata.tables.items():
                conn.execute(table.delete())
            for table_name, data in rows.items():
                if data:
                    conn.execute(self._metadata.tables[table_name].insert(), data)

        message = (
            f"Saved discovery to {self.db_path} "
            f"({len(rows['switch_summary'])} device(s), "
            f"{len(rows['lldp_neighbors'])} neighbor record(s), "
            f"{len(rows['network_connections'])} connection(s))"
        )
        if carried_links > 0:
            message += f" — {carried_links} link(s) carried over from previous runs"
        self.log_callback(message)

    @staticmethod
    def _merge_connections(existing_conns, new_conns):
        """Union of connection rows, dropping exact duplicates. Both
        directions of a link stay (the view dedups them at render time)."""
        frames = [df for df in (existing_conns, new_conns)
                  if df is not None and not df.empty]
        if not frames:
            return pd.DataFrame()
        merged = pd.concat(frames, ignore_index=True)
        return merged.drop_duplicates(
            subset=["Local IP", "Local Port", "Neighbor IP", "Neighbor Port"]
        )

    @staticmethod
    def _merge_devices(existing_devices, new_devices):
        """Union of devices keyed by IP. When an IP exists in both, the row
        with the more informative status wins (active > snmp-responding >
        unreachable), so a silent/unreachable sighting can't demote a known
        active switch."""
        priority = {
            "active": 3,
            "snmp_disabled": 2,
            "alive_snmp_silent": 2,
            "unreachable": 1,
        }
        merged = {}
        for df in (existing_devices, new_devices):
            if df is None or df.empty:
                continue
            for record in df.to_dict("records"):
                ip = str(record.get("IP", "")).strip()
                if not ip:
                    continue
                record = {k: _clean(v) for k, v in record.items()}
                prev = merged.get(ip)
                if prev is None:
                    merged[ip] = record
                else:
                    new_prio = priority.get(str(record.get("Status", "")).lower(), 0)
                    old_prio = priority.get(str(prev.get("Status", "")).lower(), 0)
                    if new_prio > old_prio:
                        merged[ip] = record
        return pd.DataFrame(list(merged.values()))

    def _read_table(self, name):
        """Reads one table as a DataFrame without its primary-key column."""
        with self._engine.connect() as conn:
            df = pd.read_sql_table(name, conn)
        if "id" in df.columns:
            df = df.drop(columns=["id"])
        return df

    @staticmethod
    def _rows_for(df):
        """Turns a DataFrame into a list of row dicts with NaN cleaned out.
        Empty tables (aka empty DataFrames) simply yield no rows."""
        if df is None or df.empty:
            return []
        records = []
        for record in df.to_dict("records"):
            records.append({k: _clean(v) for k, v in record.items()})
        return records

    # ---------------------------------------------------------------
    # Reading
    # ---------------------------------------------------------------

    def load_run(self):
        """Reads back everything saved by the most recent save_run().
        Returns a dict of DataFrames keyed by table name (empty DataFrames
        when a table has no rows yet)."""
        with self._engine.connect() as conn:
            return {
                name: pd.read_sql_table(name, conn)
                for name, table in self._metadata.tables.items()
                if name != "id"
            }

    def close(self):
        """Disposes the SQLAlchemy engine and releases all connection pools."""
        if hasattr(self, "_engine") and self._engine is not None:
            self._engine.dispose()