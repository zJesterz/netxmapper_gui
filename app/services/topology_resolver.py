"""
Topology resolution service.

Ported from the original Topology.py script. Takes the DataFrames produced
by SNMPCollector (summary_df, neighbors_df, arp_df) directly instead of
reading/writing CSV files itself — CSV/SQLite persistence is handled one
layer up, by database.py (Phase 2, step 8).
"""

import subprocess

import pandas as pd


class TopologyResolver:
    def __init__(self, manual_chassis_map=None, log_callback=None):
        """
        manual_chassis_map: dict of {normalized_mac: ip} for neighbors that
        can't be auto-resolved (same purpose as MANUAL_CHASSIS_MAP in the
        original script) — e.g. devices with SNMP disabled that never show
        up in anyone's ARP table either.

        log_callback(message: str) -> None, optional. Used to report
        unresolved chassis IDs and ARP mapping counts instead of print().
        """
        self.manual_chassis_map = manual_chassis_map or {}
        self.log_callback = log_callback or (lambda message: None)

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------

    def get_local_ips(self):
        """Detects this machine's own IPs via ipconfig, so we don't
        accidentally treat the laptop running the app as a network device."""
        ips = set()
        try:
            result = subprocess.run(
                ["ipconfig"], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if "IPv4" in line and ":" in line:
                    ip = line.split(":")[-1].strip()
                    if ip:
                        ips.add(ip)
        except Exception:
            pass
        return ips

    @staticmethod
    def normalize_mac(mac):
        return mac.strip().lower().replace("-", ":")

    # ---------------------------------------------------------------
    # Core resolution logic
    # ---------------------------------------------------------------

    def resolve(self, summary_df, neighbors_df, arp_df=None):
        """
        summary_df: from SNMPCollector.to_dataframes() — IP, Status,
            Local Chassis ID, Local SysName, Neighbors Found
        neighbors_df: from SNMPCollector.to_dataframes() — Local IP,
            Local SysName, Neighbor Chassis ID, Neighbor Management IP,
            Neighbor Port, Neighbor Port Desc, Neighbor SysName
        arp_df: from SNMPCollector.to_dataframes() — MAC, IP (optional)

        Returns (network_connections_df, devices_df, unresolved_dict).
        unresolved_dict maps normalized MAC -> info about neighbors that
        couldn't be resolved to an IP, so the caller can decide whether to
        add entries to manual_chassis_map and re-run.
        """
        local_ips = self.get_local_ips()

        summary = summary_df.copy()
        summary["Status"] = summary["Status"].astype(str).str.strip().str.lower()
        summary = summary[~summary["IP"].isin(local_ips)]

        chassis_to_ip = {}
        sysname_to_ip = {}
        for _, row in summary.iterrows():
            ip = str(row["IP"]).strip()
            chassis = str(row.get("Local Chassis ID", "")).strip()
            if chassis.lower() not in ("", "nan", "none"):
                chassis_to_ip[self.normalize_mac(chassis)] = ip
            sysname = str(row.get("Local SysName", "")).strip()
            if sysname.lower() not in ("", "nan", "none"):
                sysname_to_ip[sysname.lower()] = ip

        arp_mac_to_ip = {}
        if arp_df is not None and not arp_df.empty:
            for _, row in arp_df.iterrows():
                mac = self.normalize_mac(str(row["MAC"]).strip())
                ip = str(row["IP"]).strip()
                arp_mac_to_ip[mac] = ip
            self.log_callback(f"Loaded {len(arp_mac_to_ip)} ARP MAC -> IP mappings")

        network_connections = []
        unresolved = {}

        for _, row in neighbors_df.iterrows():
            local_ip = str(row["Local IP"]).strip()
            neighbor_chassis = str(row["Neighbor Chassis ID"]).strip()
            neighbor_port = str(row.get("Neighbor Port", "")).strip()
            neighbor_sysname = str(row.get("Neighbor SysName", "")).strip()
            neighbor_mgmt = str(row.get("Neighbor Management IP", "")).strip()
            nm = self.normalize_mac(neighbor_chassis)

            # Resolution priority: local device table -> manual overrides ->
            # neighbor's advertised management IP -> sysname match -> ARP table
            resolved_ip = chassis_to_ip.get(nm)
            if not resolved_ip:
                resolved_ip = self.manual_chassis_map.get(nm)
            if not resolved_ip and neighbor_mgmt.lower() not in ("", "nan", "none"):
                resolved_ip = neighbor_mgmt
            if not resolved_ip and neighbor_sysname.lower() not in ("", "nan", "none"):
                resolved_ip = sysname_to_ip.get(neighbor_sysname.lower())
            if not resolved_ip:
                resolved_ip = arp_mac_to_ip.get(nm)

            if not resolved_ip:
                resolved_ip = "unknown"
                if nm not in unresolved:
                    unresolved[nm] = {
                        "chassis": neighbor_chassis,
                        "sysname": neighbor_sysname,
                        "connected_from": [],
                        "ports": [],
                    }
                unresolved[nm]["connected_from"].append(local_ip)
                unresolved[nm]["ports"].append(neighbor_port)

            network_connections.append([local_ip, "", resolved_ip, neighbor_port])

        if unresolved:
            self.log_callback(f"{len(unresolved)} neighbor chassis ID(s) could not be resolved to an IP:")
            for mac, info in unresolved.items():
                self.log_callback(
                    f"  {mac} (sysName: {info['sysname'] or 'N/A'}) "
                    f"connected from {', '.join(info['connected_from'])}, "
                    f"port(s): {', '.join(info['ports'])} "
                    f"— consider adding to manual_chassis_map"
                )

        df_network = pd.DataFrame(
            network_connections,
            columns=["Local IP", "Local Port", "Neighbor IP", "Neighbor Port"]
        )
        df_network = df_network[df_network["Neighbor IP"].str.lower() != "unknown"]

        if not df_network.empty:
            df_network["pair_key"] = df_network.apply(
                lambda r: "-".join(sorted([r["Local IP"], r["Neighbor IP"]])), axis=1)
            df_network["port_key"] = df_network.apply(
                lambda r: "-".join(sorted([r["Local Port"], r["Neighbor Port"]])), axis=1)
            df_network = df_network.drop_duplicates(subset=["pair_key", "port_key"]).drop(
                columns=["pair_key", "port_key"]
            )

        devices_df = summary[["IP", "Status", "Local Chassis ID", "Local SysName"]].copy()

        return df_network, devices_df, unresolved