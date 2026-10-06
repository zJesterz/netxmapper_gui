"""
Topology view — native Qt graph rendering of the discovered Layer-2
network, mirroring the interactivity of the original web dashboard
(map.py / Cytoscape):

    - node colour by device status (blue = active, orange = SNMP-disabled,
      grey/red = unreachable); labels show sysName (or IP)
    - click a node -> rich device details in the side panel
    - click a link -> link information in the side panel
    - selecting a node highlights its connected links and shows their labels
    - nodes are draggable (links follow), mouse wheel zooms
    - Auto-Fit + PNG export
"""

import math

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFontMetrics,
    QImage,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

NODE_RADIUS = 30

STATUS_STYLES = {
    "active": (QColor("#3A7BD5"), QColor("#d9e6ff"), False),
    "snmp_disabled": (QColor("#E67E22"), QColor("#f39c12"), True),
    "alive_snmp_silent": (QColor("#E67E22"), QColor("#f39c12"), True),
    "unreachable": (QColor("#444444"), QColor("#e74c3c"), False),
}
DEFAULT_FILL = (QColor("#666666"), QColor("#ffffff"), False)

STATUS_LABELS = {
    "active": "Active (SNMP OK)",
    "snmp_disabled": "SNMP Disabled",
    "alive_snmp_silent": "SNMP Disabled",
    "unreachable": "Unreachable",
}
STATUS_COLORS = {
    "active": "#3A7BD5",
    "snmp_disabled": "#E67E22",
    "alive_snmp_silent": "#E67E22",
    "unreachable": "#e74c3c",
}


def _status_style(status):
    return STATUS_STYLES.get(status, DEFAULT_FILL)


def _clean_text(value):
    """Turn a possibly-NaN/None cell into a plain string ('' when empty)."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in ("", "nan", "none"):
        return ""
    return text


class NodeItem(QGraphicsEllipseItem):
    """A circle on the canvas representing one discovered device."""

    def __init__(self, center_x, center_y, ip, status, sysname, chassis):
        super().__init__(-NODE_RADIUS, -NODE_RADIUS, NODE_RADIUS * 2, NODE_RADIUS * 2)
        self.setPos(center_x, center_y)
        self.ip = ip
        self.status = status
        self.sysname = sysname
        self.chassis = chassis
        self._edges = []

        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setCursor(Qt.PointingHandCursor)

        fill, border, dashed = _status_style(status)
        self._brush = QBrush(fill)
        pen = QPen(border, 2)
        pen.setStyle(Qt.DashLine if dashed else Qt.SolidLine)
        self._pen = pen

        self.setBrush(self._brush)
        self.setPen(self._pen)
        self.setZValue(1)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            for edge in self._edges:
                edge.update_path()
        return super().itemChange(change, value)

    def highlight(self, selected):
        if selected:
            pen = QPen(QColor("#00e5ff"), 3)
            self.setPen(pen)
            self.setBrush(QBrush(self._brush.color().lighter(135)))
        else:
            self.setPen(self._pen)
            self.setBrush(self._brush)


class EdgeItem(QGraphicsPathItem):
    """A link between two devices. Ports are attributed to the node they
    actually belong to; both directions of the same physical link collapse
    into a single edge (matching the reference map.py grouping)."""

    def __init__(self, source_node, target_node, source_port, target_port):
        super().__init__()
        self.source_node = source_node
        self.target_node = target_node
        self.source_port = source_port
        self.target_port = target_port

        if source_port and target_port:
            self.label_text = f"{source_port}-{target_port}"
        elif source_port:
            self.label_text = source_port
        elif target_port:
            self.label_text = target_port
        else:
            self.label_text = "Unknown"

        # wide invisible hit area so links are easy to click
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.PointingHandCursor)

        self._pen = QPen(QColor("#999999"), 2)
        self._hover_pen = QPen(QColor("#cccccc"), 2.5)
        self.setPen(self._pen)
        self.setZValue(0)

        self._label = QGraphicsSimpleTextItem(self.label_text, self)
        self._label.setBrush(QBrush(QColor("#00e5ff")))
        self._label.setZValue(3)
        self._label.setVisible(False)

        self.update_path()

    def shape(self):
        """Widened hit-area so near-miss clicks also select the link."""
        stroker = QPainterPathStroker()
        stroker.setWidth(10.0)
        return stroker.createStroke(self.path())

    def update_path(self):
        a = self.source_node.sceneBoundingRect().center()
        b = self.target_node.sceneBoundingRect().center()
        path = QPainterPath()
        path.moveTo(a)
        path.lineTo(b)
        self.setPath(path)
        mid = path.pointAtPercent(0.5)
        fm = QFontMetrics(self._label.font())
        self._label.setPos(
            mid.x() - fm.horizontalAdvance(self._label.text()) / 2,
            mid.y() - fm.height() / 2,
        )

    def highlight(self, selected):
        self.setPen(QPen(QColor("#00e5ff"), 3) if selected else self._pen)
        self._label.setVisible(selected)

    def hoverEnterEvent(self, event):
        self.setPen(self._hover_pen)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        if self.pen().color() != QColor("#00e5ff"):
            self.setPen(self._pen)
        super().hoverLeaveEvent(event)


class TopologyGraphicsView(QGraphicsView):
    node_clicked = Signal(object)
    edge_clicked = Signal(object)
    empty_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        self.setBackgroundBrush(QColor("#0d0d0d"))
        self.setDragMode(QGraphicsView.NoDrag)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            item = self.itemAt(event.position().toPoint())
            if item is None:
                self.empty_clicked.emit()
            elif isinstance(item, NodeItem):
                self.node_clicked.emit(item)
            elif isinstance(item, EdgeItem):
                self.edge_clicked.emit(item)
            else:
                parent = item.parentItem()
                if isinstance(parent, NodeItem):
                    self.node_clicked.emit(parent)
                elif isinstance(parent, EdgeItem):
                    self.edge_clicked.emit(parent)
                else:
                    self.empty_clicked.emit()
        super().mousePressEvent(event)

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)
        event.accept()


class TopologyView(QWidget):
    """Tab that renders the resolved topology (devices + connections) and
    offers the map.py-style interactions: click-through details, link/device
    highlighting, draggable nodes, wheel zoom, auto-fit and PNG export."""

    def __init__(self, parent=None):
        super().__init__(parent)

        self._scene = QGraphicsScene(self)

        self._graphics_view = TopologyGraphicsView(self)
        self._graphics_view.setScene(self._scene)
        self._graphics_view.node_clicked.connect(self.select_node)
        self._graphics_view.edge_clicked.connect(self.select_edge)
        self._graphics_view.empty_clicked.connect(self.clear_selection)

        self.fit_button = QPushButton("Auto-Fit")
        self.fit_button.clicked.connect(self.auto_fit)
        self.export_button = QPushButton("Export PNG")
        self.export_button.clicked.connect(self.export_png)

        toolbar = QHBoxLayout()
        toolbar.addWidget(self.fit_button)
        toolbar.addWidget(self.export_button)
        toolbar.addStretch()

        self.details_view = QTextEdit()
        self.details_view.setReadOnly(True)
        self.details_view.setMinimumWidth(280)
        self.details_view.setMaximumWidth(340)

        graph_column = QVBoxLayout()
        graph_column.addLayout(toolbar)
        graph_column.addWidget(self._graphics_view, stretch=1)

        main = QHBoxLayout(self)
        main.addLayout(graph_column, stretch=1)
        main.addWidget(QLabel("Details:"))
        main.addWidget(self.details_view)

        self._nodes = {}
        self._edges = []
        self._selected_node = None
        self._selected_edge = None

        self.details_view.setPlainText("No topology data yet — run a discovery first.")

    # ---------------------------------------------------------------
    # Data
    # ---------------------------------------------------------------

    def set_topology(self, devices_df, df_network):
        """Rebuild the graph from the resolver's two DataFrames.
        df_network may be None/empty — the devices alone still render."""
        self._scene.clear()
        self._nodes = {}
        self._edges = []
        self._selected_node = self._selected_edge = None

        devices = list(devices_df.iterrows()) if devices_df is not None and not devices_df.empty else []

        if not devices:
            item = self._scene.addSimpleText("No topology data yet — run a discovery first.")
            item.setBrush(QBrush(QColor("#cccccc")))
            item.setPos(0, 0)
            self.details_view.setPlainText("No topology data yet — run a discovery first.")
            self.auto_fit()
            return

        # --- circular layout ---
        count = len(devices)
        radius = max(220.0, count * 45.0)
        nodes_by_ip = {}
        for idx, (_, row) in enumerate(devices):
            ip = str(row.get("IP", ""))
            status = str(row.get("Status", ""))
            sysname = _clean_text(row.get("Local SysName", ""))
            chassis = _clean_text(row.get("Local Chassis ID", ""))
            angle = (2 * math.pi * idx) / count
            cx = radius * math.cos(angle)
            cy = radius * math.sin(angle)

            node = NodeItem(cx, cy, ip, status, sysname, chassis)
            label_text = sysname or ip
            label = QGraphicsSimpleTextItem(label_text, node)
            fm = QFontMetrics(label.font())
            # position relative to the node so the label follows drags
            label.setPos(-fm.horizontalAdvance(label_text) / 2, NODE_RADIUS + 4)
            label.setBrush(QBrush(QColor("#cccccc")))
            label.setAcceptHoverEvents(False)

            self._scene.addItem(node)
            self._nodes[ip] = node
            nodes_by_ip[ip] = node

        # --- edges — group by unordered IP pair, attribute ports to the node
        # that actually owns them (identical logic to the reference map.py) ---
        edge_groups = {}
        if df_network is not None and not df_network.empty:
            for _, row in df_network.iterrows():
                source = str(row.get("Local IP", "")).strip()
                target = str(row.get("Neighbor IP", "")).strip()
                lp = str(row.get("Local Port", "")).strip()
                np = str(row.get("Neighbor Port", "")).strip()
                if not source or not target or source not in nodes_by_ip or target not in nodes_by_ip:
                    continue

                key = frozenset({source, target})
                group = edge_groups.setdefault(key, {"source": source, "target": target, "ports": {}})
                if np.lower() not in ("", "nan", "none"):
                    group["ports"][target] = np
                if lp.lower() not in ("", "nan", "none"):
                    group["ports"][source] = lp

        for group in edge_groups.values():
            source = group["source"]
            target = group["target"]
            ports = group["ports"]
            src_node = nodes_by_ip[source]
            tgt_node = nodes_by_ip[target]
            edge = EdgeItem(src_node, tgt_node, ports.get(source, ""), ports.get(target, ""))

            src_node._edges.append(edge)
            tgt_node._edges.append(edge)
            self._scene.addItem(edge)
            self._edges.append(edge)

        self.details_view.setHtml(
            self._summary_html(len(self._nodes), len(self._edges))
        )
        self.auto_fit()

    # ---------------------------------------------------------------
    # Interaction
    # ---------------------------------------------------------------

    def select_node(self, node):
        self._restore_previous()
        self._selected_node = node
        node.highlight(True)
        for edge in node._edges:
            edge.highlight(True)
        self.details_view.setHtml(self._node_html(node))

    def select_edge(self, edge):
        self._restore_previous()
        self._selected_edge = edge
        edge.highlight(True)
        self.details_view.setHtml(self._edge_html(edge))

    def clear_selection(self):
        self._restore_previous()
        self.details_view.setHtml(self._summary_html(len(self._nodes), len(self._edges)))

    def _restore_previous(self):
        if self._selected_node is not None:
            self._selected_node.highlight(False)
            for edge in self._selected_node._edges:
                edge.highlight(False)
        if self._selected_edge is not None:
            self._selected_edge.highlight(False)
        self._selected_node = None
        self._selected_edge = None

    # ---------------------------------------------------------------
    # Details panel HTML (mirrors the reference dashboard's sidebar)
    # ---------------------------------------------------------------

    @staticmethod
    def _summary_html(node_count, edge_count):
        return (
            f"<b>{node_count}</b> device(s), <b>{edge_count}</b> link(s)."
            "<br><br>Click a device or a link for details."
        )

    def _node_html(self, node):
        status_label = STATUS_LABELS.get(node.status, node.status)
        status_color = STATUS_COLORS.get(node.status, "#888888")

        blocks = [
            "<h3>Device Information</h3><hr>",
            f"<p><b>IP:</b> {node.ip}</p>",
            f'<p><b>Status:</b> <span style="color:{status_color};font-weight:600">{status_label}</span></p>',
        ]
        if node.sysname:
            blocks.append(f"<p><b>Name:</b> {node.sysname}</p>")
        if node.chassis:
            blocks.append(f"<p><b>Chassis ID:</b> {node.chassis}</p>")

        blocks.append("<br><h4>LLDP Connections</h4>")
        connection_blocks = []
        for edge in node._edges:
            if edge.source_node is node:
                neighbor = edge.target_node
                local_port = edge.source_port
                neighbor_port = edge.target_port
            else:
                neighbor = edge.source_node
                local_port = edge.target_port
                neighbor_port = edge.source_port
            connection_blocks.append(
                f'<div><p style="font-weight:600">Connected to: {neighbor.ip}</p>'
                f"<p>Local Port: {local_port or '&mdash;'}</p>"
                f"<p>Neighbor Port: {neighbor_port or '&mdash;'}</p><hr></div>"
            )

        if connection_blocks:
            blocks.extend(connection_blocks)
        else:
            blocks.append('<p style="color:#666">No LLDP connections found.</p>')

        return "".join(blocks)

    def _edge_html(self, edge):
        a, b = edge.source_node, edge.target_node
        a_name = f" ({a.sysname})" if a.sysname else ""
        b_name = f" ({b.sysname})" if b.sysname else ""
        return (
            "<h3>Link Information</h3><hr>"
            f"<p><b>Source:</b> {a.ip}{a_name}</p>"
            f"<p><b>Target:</b> {b.ip}{b_name}</p><br>"
            f'<p><b>Local Port:</b> <span style="color:#00e5ff;font-weight:600">{edge.source_port or "&mdash;"}</span></p>'
            f'<p><b>Neighbor Port:</b> <span style="color:#00e5ff;font-weight:600">{edge.target_port or "&mdash;"}</span></p>'
        )

    # ---------------------------------------------------------------
    # Actions
    # ---------------------------------------------------------------

    def auto_fit(self):
        rect = self._scene.itemsBoundingRect().adjusted(-80, -80, 80, 80)
        if rect.isNull() or rect.isEmpty():
            return
        self._graphics_view.fitInView(rect, Qt.KeepAspectRatio)

    def export_png(self):
        rect = self._scene.itemsBoundingRect().adjusted(-40, -40, 40, 40)
        if rect.isNull() or rect.isEmpty():
            return
        image = QImage(
            max(1, math.ceil(rect.width())),
            max(1, math.ceil(rect.height())),
            QImage.Format_RGB32,
        )
        image.fill(QColor("#0d0d0d"))
        painter = QPainter(image)
        self._scene.render(painter, QRectF(image.rect()), rect)
        painter.end()

        path, _ = QFileDialog.getSaveFileName(self, "Export Topology", "topology_map.png", "PNG Image (*.png)")
        if path:
            image.save(path)