import sys

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.controllers.discovery_ctrl import DiscoveryController
from app.services.config_manager import (
    effective_manual_chassis_map,
    format_manual_chassis_map,
    load_manual_chassis_map,
    parse_manual_chassis_map,
    save_manual_chassis_map,
)
from app.services.database import default_db_path
from app.views.topology_view import TopologyView

DARK_STYLESHEET = """
QMainWindow {
    background-color: #1e1e1e;
}
QTabWidget::pane {
    border: 1px solid #333333;
    background-color: #1e1e1e;
}
QTabBar::tab {
    background: #2d2d2d;
    color: #cccccc;
    padding: 8px 16px;
    border: 1px solid #333333;
    border-bottom: none;
}
QTabBar::tab:selected {
    background: #3a3a3a;
    color: #ffffff;
}
QTabBar::tab:hover {
    background: #353535;
}
QLabel {
    color: #cccccc;
}
QLineEdit {
    background: #2d2d2d;
    color: #ffffff;
    border: 1px solid #444444;
    padding: 4px;
    border-radius: 4px;
}
QPushButton {
    background: #0066ff;
    color: white;
    border: none;
    border-radius: 4px;
    padding: 8px 16px;
    font-weight: 600;
}
QPushButton:disabled {
    background: #444444;
    color: #888888;
}
QPushButton:hover:!disabled {
    background: #1a75ff;
}
QTextEdit {
    background: #141414;
    color: #cccccc;
    border: 1px solid #333333;
    font-family: Consolas, monospace;
    font-size: 12px;
}
QTableWidget {
    background: #1e1e1e;
    color: #dddddd;
    gridline-color: #333333;
    border: 1px solid #333333;
}
QHeaderView::section {
    background: #2d2d2d;
    color: #ffffff;
    padding: 4px;
    border: 1px solid #333333;
}
QProgressBar {
    border: 1px solid #444444;
    border-radius: 4px;
    text-align: center;
    color: white;
    background: #2d2d2d;
}
QProgressBar::chunk {
    background-color: #0066ff;
}
QStatusBar {
    background-color: #2d2d2d;
    color: #cccccc;
}
"""


def make_placeholder_tab(text: str) -> QWidget:
    """Simple placeholder widget until each real view is built in later phases."""
    widget = QWidget()
    layout = QVBoxLayout(widget)
    label = QLabel(text)
    label.setAlignment(Qt.AlignCenter)
    layout.addWidget(label)
    return widget


class DiscoveryTab(QWidget):
    """Lets the user enter an SNMP community string and either a subnet
    to sweep or a comma-separated list of switch IPs, then runs discovery
    on a background thread so the UI stays responsive."""

    # emitted when a discovery completes, so the main window can refresh
    # other tabs (e.g. the topology graph) with the resolved result
    result_ready = Signal(object)

    def __init__(self, status_bar, parent=None):
        super().__init__(parent)
        self.status_bar = status_bar
        self.controller = None
        self.last_result = None

        # --- Form: community string, subnet, switch IPs, manual chassis map ---
        self.community_input = QLineEdit()
        self.community_input.setPlaceholderText("e.g. public, christ, private...")

        self.subnet_input = QLineEdit()
        self.subnet_input.setPlaceholderText("e.g. 192.168.1.0/24 (leave blank if giving IPs below)")

        self.ips_input = QLineEdit()
        self.ips_input.setPlaceholderText("e.g. 192.168.1.10,192.168.1.11 (leave blank to scan subnet)")

        self.manual_map_input = QLineEdit()
        self.manual_map_input.setPlaceholderText("e.g. 00:17:7c:6b:2d:2a=192.168.1.22, 00:11:22:33:44:55=192.168.1.50")

        # Prefill manual chassis map from saved JSON config
        saved_map = load_manual_chassis_map()
        if saved_map:
            self.manual_map_input.setText(format_manual_chassis_map(saved_map))

        form = QFormLayout()
        form.addRow("Community String:", self.community_input)
        form.addRow("Subnet (CIDR):", self.subnet_input)
        form.addRow("Switch IPs (optional):", self.ips_input)
        form.addRow("Manual Chassis Map:", self.manual_map_input)

        # --- Start button + Re-resolve button + progress bar ---
        self.start_button = QPushButton("Start Discovery")
        self.start_button.clicked.connect(self.start_discovery)

        self.reresolve_button = QPushButton("Re-resolve")
        self.reresolve_button.clicked.connect(self.start_reresolve)
        self.reresolve_button.setEnabled(default_db_path().exists())

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        controls_row = QHBoxLayout()
        controls_row.addWidget(self.start_button)
        controls_row.addWidget(self.reresolve_button)
        controls_row.addWidget(self.progress_bar)

        # --- Live log ---
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)

        # --- Results table ---
        self.results_table = QTableWidget()
        self.results_table.setColumnCount(5)
        self.results_table.setHorizontalHeaderLabels(
            ["IP", "Status", "Local Chassis ID", "Local SysName", "Neighbors Found"]
        )

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(controls_row)
        layout.addWidget(QLabel("Log:"))
        layout.addWidget(self.log_view, stretch=1)
        layout.addWidget(QLabel("Results:"))
        layout.addWidget(self.results_table, stretch=2)

    def start_discovery(self):
        community = self.community_input.text().strip()
        subnet = self.subnet_input.text().strip()
        ips_raw = self.ips_input.text().strip()
        switch_ips = [ip.strip() for ip in ips_raw.split(",") if ip.strip()]

        if not community:
            QMessageBox.warning(self, "Missing community string", "Enter an SNMP community string before starting.")
            return
        if not subnet and not switch_ips:
            QMessageBox.warning(
                self, "Missing target",
                "Enter either a subnet (e.g. 192.168.1.0/24) or a comma-separated list of switch IPs."
            )
            return

        # Parse and persist manual chassis map (defaults are merged in too,
        # so resolution works even when the field is empty)
        raw_map = self.manual_map_input.text().strip()
        manual_map = effective_manual_chassis_map(raw_map)
        save_manual_chassis_map(parse_manual_chassis_map(raw_map))

        self.log_view.clear()
        self.results_table.setRowCount(0)
        self.progress_bar.setValue(0)
        self.start_button.setEnabled(False)
        self.reresolve_button.setEnabled(False)
        self.status_bar.showMessage("Discovery running...")

        self.controller = DiscoveryController(
            community=community,
            subnet=subnet or None,
            switch_ips=switch_ips,
            manual_chassis_map=manual_map,
        )
        self.controller.progress.connect(self.on_progress)
        self.controller.log.connect(self.on_log)
        self.controller.finished_ok.connect(self.on_finished)
        self.controller.failed.connect(self.on_failed)
        self.controller.start()

    def start_reresolve(self):
        # Parse and persist manual chassis map (defaults merged in), then re-resolve
        # from the stored discovery data
        raw_map = self.manual_map_input.text().strip()
        manual_map = effective_manual_chassis_map(raw_map)
        save_manual_chassis_map(parse_manual_chassis_map(raw_map))

        self.log_view.clear()
        self.progress_bar.setValue(0)
        self.start_button.setEnabled(False)
        self.reresolve_button.setEnabled(False)
        self.status_bar.showMessage("Re-resolving topology from stored database...")

        self.controller = DiscoveryController.create_reresolve(manual_chassis_map=manual_map)
        self.controller.progress.connect(self.on_progress)
        self.controller.log.connect(self.on_log)
        self.controller.finished_ok.connect(self.on_finished)
        self.controller.failed.connect(self.on_failed)
        self.controller.start()

    def on_progress(self, current, total, message):
        pct = int((current / total) * 100) if total else 0
        self.progress_bar.setValue(pct)
        self.status_bar.showMessage(message)

    def on_log(self, message):
        self.log_view.append(message)

    def on_finished(self, result):
        self.start_button.setEnabled(True)
        self.reresolve_button.setEnabled(True)
        self.progress_bar.setValue(100)
        self.status_bar.showMessage(f"Discovery complete — {len(result.summary_df)} device(s) found")

        self.results_table.setRowCount(len(result.summary_df))
        for row_idx, (_, row) in enumerate(result.summary_df.iterrows()):
            values = [
                str(row.get("IP", "")),
                str(row.get("Status", "")),
                str(row.get("Local Chassis ID", "") or ""),
                str(row.get("Local SysName", "") or ""),
                str(row.get("Neighbors Found", "")),
            ]
            for col_idx, value in enumerate(values):
                self.results_table.setItem(row_idx, col_idx, QTableWidgetItem(value))

        self.results_table.resizeColumnsToContents()

        if result.df_network is not None and not result.df_network.empty:
            self.log_view.append(f"Topology resolved: {len(result.df_network)} connection(s), "
                                 f"{len(result.devices_df)} device(s)")
        if result.unresolved:
            self.log_view.append(f"{len(result.unresolved)} neighbor chassis ID(s) could not be "
                                 "resolved to an IP (details above before this line).")
        if result.db_path:
            self.log_view.append(f"Discovery saved to {result.db_path}")

        self.last_result = result
        self.result_ready.emit(result)

    def on_failed(self, error_message):
        self.start_button.setEnabled(True)
        self.reresolve_button.setEnabled(default_db_path().exists())
        self.status_bar.showMessage("Discovery failed")
        QMessageBox.critical(self, "Discovery failed", error_message)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Layer-2 Devices Topology Visualization and Anomaly Detection")
        self.resize(1280, 800)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.status_bar = self.statusBar()
        self.status_bar.showMessage("Ready — no devices discovered yet")

        self.discovery_tab = DiscoveryTab(self.status_bar)
        self.topology_view = TopologyView()
        self.discovery_tab.result_ready.connect(self.on_discovery_result)

        self.tabs.addTab(self.discovery_tab, "Discovery")
        self.tabs.addTab(self.topology_view, "Topology")
        self.tabs.addTab(make_placeholder_tab("Anomaly Dashboard — coming in Phase 8"), "Anomalies")
        self.tabs.addTab(make_placeholder_tab("Console / Logs — coming in Phase 4"), "Console")

    def on_discovery_result(self, result):
        if result.df_network is not None and result.devices_df is not None:
            self.topology_view.set_topology(result.devices_df, result.df_network)
            self.tabs.setCurrentWidget(self.topology_view)
            self.status_bar.showMessage(
                f"Topology updated — {len(result.devices_df)} device(s), "
                f"{len(result.df_network)} link(s)"
            )


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_STYLESHEET)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())