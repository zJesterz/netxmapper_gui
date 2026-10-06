"""
Discovery controller — Phase 3 threading layer.

Wraps SNMPCollector (app/services/snmp_collector.py) in a QThread so the
GUI never blocks while pinging/SNMP-walking a subnet. Translates the
collector's plain progress_callback/log_callback into real Qt signals
that the main window can connect to.

After collecting the raw data, the worker thread also resolves the LLDP
neighbor chassis IDs into a topology (TopologyResolver) and persists the
whole run to SQLite (Database) — all off the GUI thread. The resolved
pieces are handed to the view together as a single DiscoveryResult.

Also supports offline re-resolution (mode="reresolve") from stored database
runs with updated manual chassis maps without re-scanning the network.
"""

import asyncio
from dataclasses import dataclass

from PySide6.QtCore import QThread, Signal

from app.services.database import Database
from app.services.snmp_collector import SNMPCollector
from app.services.topology_resolver import TopologyResolver


@dataclass
class DiscoveryResult:
    """Everything produced by one discovery run, wrapped for the worker
    thread -> GUI signal bridge."""
    summary_df: object
    neighbors_df: object
    arp_df: object
    df_network: object
    devices_df: object
    unresolved: dict
    db_path: str


class DiscoveryController(QThread):
    # (current, total, message)
    progress = Signal(int, int, str)
    # single log line
    log = Signal(str)
    # emitted once at the very end, carrying a DiscoveryResult object
    finished_ok = Signal(object)
    # emitted if anything raises inside the worker thread
    failed = Signal(str)

    def __init__(
        self,
        community=None,
        subnet=None,
        switch_ips=None,
        manual_chassis_map=None,
        mode="discovery",
        db_path=None,
        parent=None,
    ):
        super().__init__(parent)
        self.community = community
        self.subnet = subnet
        self.switch_ips = switch_ips or []
        self.manual_chassis_map = manual_chassis_map or {}
        self.mode = mode
        self.db_path = db_path

    @classmethod
    def create_reresolve(cls, manual_chassis_map=None, db_path=None, parent=None):
        """Factory method to create a controller configured for offline re-resolution."""
        return cls(
            manual_chassis_map=manual_chassis_map,
            mode="reresolve",
            db_path=db_path,
            parent=parent,
        )

    def run(self):
        """QThread entry point."""
        if self.mode == "reresolve":
            self._run_reresolve()
            return

        # Discovery mode runs asyncio event loop for SNMP collector
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run_discovery())
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            loop.close()

    async def _run_discovery(self):
        collector = SNMPCollector(
            community=self.community,
            progress_callback=lambda cur, total, msg: self.progress.emit(cur, total, msg),
            log_callback=lambda msg: self.log.emit(msg),
        )

        switch_ips = list(self.switch_ips)
        silent_ips = []

        if not switch_ips:
            if not self.subnet:
                self.failed.emit("No switch IPs or subnet provided.")
                return
            self.log.emit(f"No switch IPs given — scanning subnet {self.subnet}...")
            switch_ips, silent_ips = await collector.discover_switches(self.subnet)

            if not switch_ips and not silent_ips:
                self.failed.emit(f"No live hosts found in {self.subnet}.")
                return

        summary_df, neighbors_df, arp_df = await collector.run_full_discovery(switch_ips, silent_ips)

        df_network = None
        devices_df = None
        unresolved = {}
        db_path = ""

        try:
            resolver = TopologyResolver(
                manual_chassis_map=self.manual_chassis_map,
                log_callback=lambda msg: self.log.emit(msg),
            )
            df_network, devices_df, unresolved = resolver.resolve(summary_df, neighbors_df, arp_df)
            self.log.emit(
                f"Resolved {len(df_network)} network connection(s) across {len(devices_df)} device(s)"
            )
        except Exception as exc:
            self.log.emit(f"Topology resolution failed ({exc}) — continuing with raw data only.")

        database = None
        try:
            database = Database(db_path=self.db_path, log_callback=lambda msg: self.log.emit(msg))
            database.save_run(summary_df, neighbors_df, arp_df, df_network, devices_df)
            db_path = str(database.db_path)
        except Exception as exc:
            self.log.emit(f"Saving to database failed ({exc}) — discovery data only kept in memory.")
        finally:
            if database:
                database.close()

        self.finished_ok.emit(
            DiscoveryResult(
                summary_df=summary_df,
                neighbors_df=neighbors_df,
                arp_df=arp_df,
                df_network=df_network,
                devices_df=devices_df,
                unresolved=unresolved,
                db_path=db_path,
            )
        )

    def _run_reresolve(self):
        database = None
        try:
            self.progress.emit(10, 100, "Loading stored run from database...")
            self.log.emit("Loading stored discovery data from database...")
            database = Database(db_path=self.db_path, log_callback=lambda msg: self.log.emit(msg))
            data = database.load_run()

            summary_df = data.get("switch_summary")
            neighbors_df = data.get("lldp_neighbors")
            arp_df = data.get("arp_mappings")

            if summary_df is None or summary_df.empty:
                self.failed.emit("No stored discovery run found in database.")
                return

            # Drop database primary key columns if present
            if "id" in summary_df.columns:
                summary_df = summary_df.drop(columns=["id"])
            if neighbors_df is not None and "id" in neighbors_df.columns:
                neighbors_df = neighbors_df.drop(columns=["id"])
            if arp_df is not None and "id" in arp_df.columns:
                arp_df = arp_df.drop(columns=["id"])

            self.progress.emit(50, 100, "Re-resolving topology...")
            resolver = TopologyResolver(
                manual_chassis_map=self.manual_chassis_map,
                log_callback=lambda msg: self.log.emit(msg),
            )
            df_network, devices_df, unresolved = resolver.resolve(summary_df, neighbors_df, arp_df)
            self.log.emit(
                f"Re-resolved {len(df_network)} network connection(s) across {len(devices_df)} device(s)"
            )

            self.progress.emit(80, 100, "Saving updated topology to database...")
            database.save_run(summary_df, neighbors_df, arp_df, df_network, devices_df)
            db_path = str(database.db_path)

            self.progress.emit(100, 100, "Re-resolve complete.")
            self.finished_ok.emit(
                DiscoveryResult(
                    summary_df=summary_df,
                    neighbors_df=neighbors_df,
                    arp_df=arp_df,
                    df_network=df_network,
                    devices_df=devices_df,
                    unresolved=unresolved,
                    db_path=db_path,
                )
            )
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if database:
                database.close()