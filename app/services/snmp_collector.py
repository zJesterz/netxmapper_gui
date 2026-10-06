"""
SNMP collection service.

Ported from the original chasis.py script. Kept free of any PySide6/Qt
imports on purpose — this class is meant to be reusable and unit-testable
on its own. The QThread wrapper (app/controllers/discovery_ctrl.py, built
in Phase 3) is responsible for calling into this class from a worker
thread and turning progress_callback/log_callback invocations into real
Qt signals (progress(int), device_found(dict), etc).
"""

from pysnmp.hlapi.asyncio import *
import asyncio
import subprocess
import platform
import ipaddress
import pandas as pd


class SNMPCollector:
    # LLDP local + remote table OIDs
    OID_LOC_CHASSIS_ID = "1.0.8802.1.1.2.1.3.2.0"
    OID_LOC_SYS_NAME = "1.0.8802.1.1.2.1.3.3.0"

    OID_REM_CHASSIS_ID = "1.0.8802.1.1.2.1.4.1.1.5"
    OID_REM_PORT_ID = "1.0.8802.1.1.2.1.4.1.1.7"
    OID_REM_PORT_DESC = "1.0.8802.1.1.2.1.4.1.1.8"
    OID_REM_SYS_NAME = "1.0.8802.1.1.2.1.4.1.1.9"

    # lldpRemManAddrTable: carries the neighbor's advertised management IP.
    # Unlike other LLDP columns, the address itself is embedded IN THE OID
    # INDEX, not the returned value — see parse_management_ip() below.
    OID_REM_MAN_ADDR_IF_SUBTYPE = "1.0.8802.1.1.2.1.4.2.1.2"

    # ARP table OID: ipNetToMediaPhysAddress -- maps IP -> MAC on active switches.
    # Indexed by (ifIndex, ipAddress), so the full OID is:
    #   1.3.6.1.2.1.4.22.1.2.<ifIndex>.<a>.<b>.<c>.<d>  (for IPv4)
    # Value is the MAC address as raw bytes.
    OID_ARP_MAC = "1.3.6.1.2.1.4.22.1.2"

    def __init__(
        self,
        community="christ",
        port=161,
        batch_size=50,
        timeout=1,
        retries=1,
        progress_callback=None,
        log_callback=None,
    ):
        """
        progress_callback(current: int, total: int, message: str) -> None
        log_callback(message: str) -> None

        Both are optional. If not supplied, they're no-ops — useful for
        running this class in tests or a plain script without a GUI.
        """
        self.community = community
        self.port = port
        self.batch_size = batch_size
        self.timeout = timeout
        self.retries = retries
        self.progress_callback = progress_callback or (lambda current, total, message: None)
        self.log_callback = log_callback or (lambda message: None)

    # ---------------------------------------------------------------
    # Low-level helpers
    # ---------------------------------------------------------------

    def ping_ip(self, ip):
        param = "-n" if platform.system().lower() == "windows" else "-c"
        try:
            result = subprocess.run(
                ["ping", param, "1", "-w", "2000", ip],
                capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False

    def octets_to_str(self, value):
        """Best-effort conversion of an OctetString to a readable string.
        Falls back to a MAC-style hex string if it looks like raw bytes,
        otherwise returns the printable string as-is."""
        try:
            raw = value.asOctets()
        except Exception:
            return str(value)

        try:
            text = raw.decode("utf-8")
            if text.isprintable():
                return text
        except Exception:
            pass

        return ':'.join(f'{b:02x}' for b in raw)

    async def get_single(self, target, oid):
        errorIndication, errorStatus, errorIndex, varBinds = await getCmd(
            SnmpEngine(),
            CommunityData(self.community, mpModel=1),
            UdpTransportTarget((target, self.port), timeout=self.timeout, retries=self.retries),
            ContextData(),
            ObjectType(ObjectIdentity(oid))
        )
        if errorIndication or errorStatus:
            return None
        for varBind in varBinds:
            return varBind[1]
        return None

    async def walk_column(self, target, base_oid, max_rows=200):
        """Walks an SNMP table column by repeatedly calling nextCmd.

        IMPORTANT: OIDs are compared as numeric tuples, not strings.
        pysnmp resolves the leading '1' to its MIB symbolic name 'iso'
        when stringified, which silently breaks string-prefix matching
        against base_oid."""
        results = {}
        snmpEngine = SnmpEngine()
        base_tuple = tuple(int(x) for x in base_oid.split("."))
        current_oid = ObjectIdentity(base_oid)

        for _ in range(max_rows):
            errorIndication, errorStatus, errorIndex, varBinds = await nextCmd(
                snmpEngine,
                CommunityData(self.community, mpModel=1),
                UdpTransportTarget((target, self.port), timeout=self.timeout, retries=self.retries),
                ContextData(),
                ObjectType(current_oid),
                lexicographicMode=False
            )

            if errorIndication or errorStatus:
                break
            if not varBinds:
                break

            row = varBinds[0]
            try:
                varBind = row[0]
                pair = list(varBind)
            except Exception:
                break

            if len(pair) < 2:
                break

            oid, value = pair[0], pair[1]
            oid_tuple = tuple(oid)

            if oid_tuple[:len(base_tuple)] != base_tuple:
                break

            suffix = ".".join(str(x) for x in oid_tuple[len(base_tuple):])
            results[suffix] = value
            current_oid = ObjectIdentity(oid)

        return results

    def parse_management_ip(self, suffix):
        """Parses a lldpRemManAddrTable index suffix to extract an embedded
        IPv4 address, if present.

        Index format per LLDP-MIB: timeMark.localPort.remIndex.addrSubtype.addrLen.<addr octets>
        For IPv4: addrSubtype=1, addrLen=4, followed by the 4 IP octets.

        Returns (base_suffix, ip_string_or_None).
        """
        parts = suffix.split(".")
        if len(parts) < 5:
            return suffix, None

        base_suffix = ".".join(parts[:3])
        addr_subtype = parts[3]
        addr_len = int(parts[4])

        if addr_subtype == "1" and addr_len == 4 and len(parts) >= 5 + addr_len:
            ip_octets = parts[5:5 + addr_len]
            return base_suffix, ".".join(ip_octets)

        return base_suffix, None

    # ---------------------------------------------------------------
    # Per-device queries
    # ---------------------------------------------------------------

    async def get_arp_mappings(self, switch_ips, max_entries=2000):
        """Walks the ARP table on each active switch and returns a dict of
        {normalized_mac: ip} mappings. This lets us resolve the MAC/chassis ID
        of SNMP-disabled switches — their IP is known from ping, their MAC is
        known from other switches' ARP tables, and their chassis ID (same as
        the bridge MAC) appears in other switches' LLDP tables."""
        mac_to_ip = {}
        for sw_ip in switch_ips:
            entries = await self.walk_column(sw_ip, self.OID_ARP_MAC, max_rows=max_entries)
            for suffix, value in entries.items():
                parts = suffix.split(".")
                if len(parts) < 5:
                    continue
                ip_addr = ".".join(parts[-4:])
                mac = self.octets_to_str(value)
                mac_to_ip[mac] = ip_addr
        return mac_to_ip

    async def get_lldp_neighbors(self, ip):
        """Returns local chassis/sysname plus a list of remote neighbor dicts."""
        loc_chassis = await self.get_single(ip, self.OID_LOC_CHASSIS_ID)
        loc_sysname = await self.get_single(ip, self.OID_LOC_SYS_NAME)

        rem_chassis = await self.walk_column(ip, self.OID_REM_CHASSIS_ID)
        rem_port = await self.walk_column(ip, self.OID_REM_PORT_ID)
        rem_port_desc = await self.walk_column(ip, self.OID_REM_PORT_DESC)
        rem_sysname = await self.walk_column(ip, self.OID_REM_SYS_NAME)
        rem_man_addr_raw = await self.walk_column(ip, self.OID_REM_MAN_ADDR_IF_SUBTYPE)

        rem_man_ip = {}
        for full_suffix in rem_man_addr_raw:
            base_suffix, parsed_ip = self.parse_management_ip(full_suffix)
            if parsed_ip:
                rem_man_ip[base_suffix] = parsed_ip

        neighbors = []
        for suffix in rem_chassis:
            neighbors.append({
                "Local IP": ip,
                "Local SysName": self.octets_to_str(loc_sysname) if loc_sysname else None,
                "Neighbor Chassis ID": self.octets_to_str(rem_chassis[suffix]),
                "Neighbor Management IP": rem_man_ip.get(suffix),
                "Neighbor Port": self.octets_to_str(rem_port[suffix]) if suffix in rem_port else None,
                "Neighbor Port Desc": self.octets_to_str(rem_port_desc[suffix]) if suffix in rem_port_desc else None,
                "Neighbor SysName": self.octets_to_str(rem_sysname[suffix]) if suffix in rem_sysname else None,
            })

        return {
            "ip": ip,
            "local_chassis_id": self.octets_to_str(loc_chassis) if loc_chassis else None,
            "local_sysname": self.octets_to_str(loc_sysname) if loc_sysname else None,
            "neighbors": neighbors,
        }

    async def scan_switch(self, ip):
        if not self.ping_ip(ip):
            self.log_callback(f"{ip} --> unreachable (ping failed)")
            return {"ip": ip, "status": "unreachable", "data": None}

        data = await self.get_lldp_neighbors(ip)

        if data["local_chassis_id"] is None and not data["neighbors"]:
            self.log_callback(f"{ip} --> reachable, but SNMP/LLDP not responding")
            return {"ip": ip, "status": "snmp_disabled", "data": data}

        self.log_callback(f"{ip} --> active, {len(data['neighbors'])} LLDP neighbor(s) found")
        return {"ip": ip, "status": "active", "data": data}

    # ---------------------------------------------------------------
    # Discovery + full scan
    # ---------------------------------------------------------------

    async def discover_switches(self, subnet):
        """Ping-sweeps a subnet. Returns two lists:
        - snmp_ips: hosts that respond to SNMP lldpLocChassisId (queryable directly)
        - silent_ips: hosts that are alive/pingable but don't respond to SNMP
          (kept instead of discarded, since they may still show up as a
          neighbor in another switch's LLDP table)."""
        subnet_obj = ipaddress.IPv4Network(subnet)
        ip_list = [
            str(ip) for ip in subnet_obj
            if ip != subnet_obj.network_address and ip != subnet_obj.broadcast_address
        ]
        self.log_callback(f"Pinging {len(ip_list)} addresses in {subnet}...")

        snmp_ips = []
        silent_ips = []
        total = len(ip_list)
        scanned = 0

        for i in range(0, len(ip_list), self.batch_size):
            batch = ip_list[i:i + self.batch_size]
            alive_flags = await asyncio.gather(
                *[asyncio.to_thread(self.ping_ip, ip) for ip in batch]
            )
            alive_ips = [ip for ip, alive in zip(batch, alive_flags) if alive]

            scanned += len(batch)
            self.progress_callback(scanned, total, f"Pinged {scanned}/{total}")

            if not alive_ips:
                continue

            chassis_results = await asyncio.gather(
                *[self.get_single(ip, self.OID_LOC_CHASSIS_ID) for ip in alive_ips]
            )

            for ip, chassis in zip(alive_ips, chassis_results):
                if chassis is not None:
                    self.log_callback(f"{ip} --> responds to SNMP/LLDP, treating as a switch")
                    snmp_ips.append(ip)
                else:
                    self.log_callback(f"{ip} --> alive, but no SNMP/LLDP response (keeping as candidate)")
                    silent_ips.append(ip)

        return snmp_ips, silent_ips

    async def scan_all(self, switch_ips):
        """Scans every switch IP for LLDP data, reporting progress as it goes.
        Returns the raw list of per-device result dicts (same shape as
        scan_switch's return value) — caller decides how to turn this into
        DataFrames/DB rows via to_dataframes()."""
        total = len(switch_ips)
        results = []
        for idx, ip in enumerate(switch_ips, start=1):
            result = await self.scan_switch(ip)
            results.append(result)
            self.progress_callback(idx, total, f"Scanned {ip} ({idx}/{total})")
        return results

    # ---------------------------------------------------------------
    # Output shaping
    # ---------------------------------------------------------------

    def to_dataframes(self, results, silent_ips=None, arp_mappings=None):
        """Converts scan_all() results (+ optional silent_ips/arp_mappings)
        into the same three DataFrames the original script wrote to CSV:
        summary_df, neighbors_df, arp_df. Saving to CSV/SQLite is left to
        the caller (database.py / config_manager.py in later phases)."""
        silent_ips = silent_ips or []
        arp_mappings = arp_mappings or {}

        summary_rows = []
        all_neighbors = []

        for r in results:
            summary_rows.append({
                "IP": r["ip"],
                "Status": r["status"],
                "Local Chassis ID": r["data"]["local_chassis_id"] if r["data"] else None,
                "Local SysName": r["data"]["local_sysname"] if r["data"] else None,
                "Neighbors Found": len(r["data"]["neighbors"]) if r["data"] else 0,
            })
            if r["data"]:
                all_neighbors.extend(r["data"]["neighbors"])

        for ip in silent_ips:
            summary_rows.append({
                "IP": ip,
                "Status": "alive_snmp_silent",
                "Local Chassis ID": None,
                "Local SysName": None,
                "Neighbors Found": 0,
            })

        summary_df = pd.DataFrame(summary_rows)
        neighbors_df = pd.DataFrame(all_neighbors)
        arp_df = pd.DataFrame([
            {"MAC": mac, "IP": ip} for mac, ip in arp_mappings.items()
        ])

        return summary_df, neighbors_df, arp_df

    # ---------------------------------------------------------------
    # High-level convenience entry point
    # ---------------------------------------------------------------

    async def run_full_discovery(self, switch_ips, silent_ips=None):
        """Runs the complete pipeline used by the original main():
        scan every switch, walk ARP tables, and shape the results into
        the three DataFrames. This is what discovery_ctrl.py (Phase 3)
        will call from inside its QThread."""
        silent_ips = silent_ips or []

        results = await self.scan_all(switch_ips)

        self.log_callback("Querying ARP tables on active switches to resolve MAC -> IP mappings...")
        arp_mappings = await self.get_arp_mappings(switch_ips)
        if arp_mappings:
            self.log_callback(f"Found {len(arp_mappings)} MAC -> IP entries")

        summary_df, neighbors_df, arp_df = self.to_dataframes(results, silent_ips, arp_mappings)
        return summary_df, neighbors_df, arp_df