import QtQuick
import Quickshell
import Quickshell.Hyprland
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

// Usage Notch — pill en el borde derecho con los límites de uso de los
// asistentes de código. Inspirado en Codenotch (ver NOTICE.md).
//
// v1: pill colapsada siempre visible + tooltip al hover con ventanas y
// resets. Los datos llegan de usage.py (poll cada 60 s + refresh al expandir).
Item {
  id: root

  // El backend vive junto a este fichero: el plugin corre desde donde esté
  // instalado sin poner nada en $PATH (patrón de notification-center).
  readonly property string script:
    Qt.resolvedUrl("./usage.py").toString().replace(/^file:\/\//, "")

  property var providers: []
  property var activeProviders: []
  property bool expanded: false
  property bool settingsMode: false
  property string lastError: ""
  property double lastPoll: 0

  // Placement (vive en config.json, dueño: usage.py `config set`).
  property string placementScreen: "auto"
  property string placementEdge: "right"
  property string primaryName: ""
  readonly property bool isRight: root.placementEdge !== "left"
  readonly property string configPath: {
    var xdg = Quickshell.env("XDG_CONFIG_HOME")
    var base = (xdg && xdg !== "") ? xdg : Quickshell.env("HOME") + "/.config"
    return base + "/synapsync-usage-notch/config.json"
  }

  readonly property int pillWidth: Style.space(44)
  readonly property int rowH: Style.space(44)
  readonly property int cardWidth: Style.space(360)
  readonly property int gap: Style.space(10)
  readonly property int pad: Style.space(12)
  readonly property int pillHeight: Math.max(
    activeProviders.length * root.rowH + root.pad * 2, root.rowH + root.pad * 2)

  function headline(p) {
    if (!p || !p.windows || p.windows.length === 0) return null
    // El anillo es una ventana con límite; el conteo (~tokens) no llena nada.
    var best = null, first = p.windows[0]
    for (var i = 0; i < p.windows.length; i++) {
      var w = p.windows[i]
      if (w.usedFraction === null || w.usedFraction === undefined) continue
      if (!best || w.usedFraction > best.usedFraction) best = w
    }
    return best || first
  }

  function compact(n) {
    if (n === null || n === undefined) return "–"
    if (n < 1000) return String(Math.round(n))
    var units = [[1e9, "B"], [1e6, "M"], [1e3, "K"]]
    for (var i = 0; i < units.length; i++) {
      if (n >= units[i][0]) {
        var v = n / units[i][0]
        return (v >= 100 ? Math.round(v) : Math.round(v * 10) / 10) + units[i][1]
      }
    }
    return String(Math.round(n))
  }

  function money(n) {
    return "$" + (Math.round((+n || 0) * 100) / 100).toFixed(2)
  }

  function countText(w) {
    if (w.unit === "usd") return root.money(w.count)
    return "~" + root.compact(w.count)
  }

  function windowLine(w) {
    var head = (w.usedFraction === null || w.usedFraction === undefined)
      ? root.countText(w)
      : Math.round(w.usedFraction * 100) + "%"
    return (w.label || "?") + " — " + head + " · " + root.resetCopy(w.resetsAt)
  }

  function bandColor(frac, dimmed) {
    var c = frac >= 0.8 ? "#e5484d" : frac >= 0.5 ? "#f5a524" : "#46a758"
    return dimmed ? Qt.darker(c, 1.6) : c
  }

  function resetCopy(resetsAt) {
    if (!resetsAt) return "no reset info"
    var ms = resetsAt - Date.now()
    if (ms <= 0) return "rolling over…"
    var m = Math.floor(ms / 60000)
    if (m < 60) return "Resets in " + m + "m"
    var h = Math.floor(m / 60)
    if (h < 48) return "Resets in " + h + "h" + (m % 60 ? " " + (m % 60) + "m" : "")
    return "Resets in " + Math.floor(h / 24) + "d " + (h % 24) + "h"
  }

  // Edad de la última lectura buena (los snaps stale conservan su fetchedAt).
  function ageCopy(ts) {
    if (!ts) return ""
    var s = Math.max(0, Math.floor((Date.now() - ts) / 1000))
    if (s < 90) return "just now"
    var m = Math.floor(s / 60)
    if (m < 90) return m + "m ago"
    var h = Math.floor(m / 60)
    if (h < 60) return h + "h ago"
    return Math.floor(h / 24) + "d ago"
  }

  // Marcador de versión viva: confirma en el log qué código corre la shell.
  Component.onCompleted: console.log("notch: loaded v7-polish")

  // Pantalla viva por NOMBRE: los objetos Screen mueren entre queries y
  // reasignar panel.screen recrea la superficie (parpadeo/muerte). Solo se
  // toca cuando cambia el nombre deseado; null = Quickshell elige.
  property var targetScreen: null
  property string appliedScreenName: "##none##"
  function screenName(s) { return (s && s.name) ? String(s.name) : "" }
  function screenNames() {
    var out = [], screens = Quickshell.screens
    for (var i = 0; i < screens.length; i++) {
      if (screens[i]) out.push(String(screens[i].name || ""))
    }
    return out
  }
  function findScreen(name) {
    var screens = Quickshell.screens
    for (var i = 0; i < screens.length; i++) {
      if (screens[i] && String(screens[i].name || "") === name) return screens[i]
    }
    return null
  }
  function desiredScreenName() {
    var want = root.placementScreen || "auto"
    if (want === "auto") return ""
    if (want === "primary") { root.snapshotPrimary(); return root.primaryName || "" }
    return want
  }
  function reconcileScreen(reason) {
    var names = root.screenNames()
    var want = root.desiredScreenName()
    var targetName = (want !== "" && names.indexOf(want) !== -1) ? want : ""
    if (targetName === root.appliedScreenName) return
    root.targetScreen = targetName !== "" ? root.findScreen(targetName) : null
    root.appliedScreenName = targetName
    console.log("notch: screen ->", targetName !== "" ? targetName : "auto",
      "(" + reason + ")")
  }

  // Foto estable del monitor con foco (solo al leer config o al pedirla):
  // suscribirse al foco en un binding haría saltar la pill al cambiar de
  // ventana, que es justo lo que se quiere evitar.
  function snapshotPrimary() {
    var name = ""
    try {
      var mon = Hyprland.focusedMonitor
      if (mon && mon.name) name = String(mon.name)
    } catch (e) {}
    root.primaryName = name
  }

  function applyConfig(text) {
    var cfg = {}
    try { cfg = JSON.parse(text || "{}") } catch (e) { cfg = {} }
    var pl = (cfg && cfg.placement) || {}
    root.placementScreen =
      (typeof pl.screen === "string" && pl.screen !== "") ? pl.screen : "auto"
    root.placementEdge = pl.edge === "left" ? "left" : "right"
    root.snapshotPrimary()
    root.reconcileScreen("config")
  }

  function screenOptions() {
    var opts = ["auto", "primary"]
    var screens = Quickshell.screens
    for (var i = 0; i < screens.length; i++) {
      var n = screens[i] ? String(screens[i].name || "") : ""
      if (n !== "" && opts.indexOf(n) === -1) opts.push(n)
    }
    return opts
  }

  function cycleScreen() {
    var opts = root.screenOptions()
    var idx = opts.indexOf(root.placementScreen || "auto")
    var next = opts[(idx + 1) % opts.length]
    root.placementScreen = next
    root.configSet("placement.screen", next)
    root.reconcileScreen("cycle")
  }

  function toggleEdge() {
    var next = root.placementEdge === "left" ? "right" : "left"
    root.placementEdge = next
    root.configSet("placement.edge", next)
  }

  function open() { root.expanded = true }
  function close() { root.expanded = false; root.settingsMode = false }
  function subLine(p) {
    var s = p.status || "ok"
    if (s === "disabled") return "off — click to enable"
    if (s === "stale") return "on · stale · " + root.ageCopy(p.fetchedAt)
    if (s === "needsAuth") return "on · sign in needed"
    if (s === "error") return "on · " + (p.error || "error")
    return "on"
  }
  function toggle() { root.expanded ? root.close() : root.open() }
  function ping(): string { return "ok" }
  function state(): string { return root.expanded ? "open" : "closed" }

  // ------------------------------------------------------------ datos
  function absorb(text) {
    var data
    try {
      data = JSON.parse(text)
    } catch (e) {
      root.lastError = "bad JSON from backend"
      return
    }
    if (!Array.isArray(data)) return
    root.applyProviders(data)
    root.lastPoll = Date.now()
    root.lastError = ""
  }

  // Fuente única de verdad para la lista (el Repeater solo reacciona a
  // reasignación: mutar objetos in-place no notifica a los bindings).
  function applyProviders(data) {
    root.providers = data
    root.activeProviders = data.filter(function(p) {
      return (p.status || "ok") !== "disabled"
    })
    ringRepaint()
  }

  // Feedback optimista: el toggle se refleja al instante; el poll de red
  // solo rellena datos después. Sin esto, el click espera ~3 s al poll.
  function flipLocal(pid) {
    var next = []
    for (var i = 0; i < root.providers.length; i++) {
      var p = root.providers[i], c = {}
      for (var k in p) c[k] = p[k]
      if (p.id === pid) {
        if ((p.status || "ok") === "disabled") {
          c.status = "stale"; c.error = "refreshing…"
        } else {
          c.status = "disabled"; c.windows = []
          c.error = "off in settings — enable to resume"
        }
      }
      next.push(c)
    }
    root.applyProviders(next)
  }

  function ringRepaint() {
    for (var i = 0; i < ringRepeater.count; i++) {
      var cell = ringRepeater.itemAt(i)
      if (cell && cell.ring) cell.ring.requestPaint()
    }
  }

  Process {
    id: pollProc
    stdout: StdioCollector {
      onStreamFinished: {
        root.absorb(text)
        if (root.pendingPoll) { root.pendingPoll = false; root.poll() }
      }
    }
  }

  property bool pendingPoll: false

  function poll() {
    // Si el poll periódico va en curso, encolar: si no, el refresh del
    // toggle se perdería y la UI tardaría hasta 60 s en actualizarse.
    if (pollProc.running) { root.pendingPoll = true; return }
    pollProc.command = ["python3", root.script, "poll"]
    pollProc.running = true
  }

  Process {
    id: cfgProc
    stdout: StdioCollector {
      onStreamFinished: root.poll()
    }
  }

  function toggleProvider(pid) {
    if (cfgProc.running) return
    root.flipLocal(pid)
    cfgProc.command = ["python3", root.script, "config", "toggle", String(pid)]
    cfgProc.running = true
  }

  function configSet(key, value) {
    if (cfgProc.running) return
    cfgProc.command = ["python3", root.script, "config", "set", String(key), String(value)]
    cfgProc.running = true
  }

  // Lee config.json en vivo (el backend escribe atómico: .tmp + rename,
  // así el watch nunca lee medio fichero).
  FileView {
    id: configFile
    path: root.configPath
    watchChanges: true
    atomicWrites: true
    printErrors: false
    onLoaded: root.applyConfig(text())
  }

  Timer {
    id: pollTimer
    interval: 60000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.poll()
  }

  // Guardián de pantalla: JS puro cada 10 s, sin red ni spam (solo loguea
  // cambios). Cura la muerte silenciosa tras hotplug/suspend DPMS.
  Timer {
    id: screenGuard
    interval: 10000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.reconcileScreen("tick")
  }

  // UX v4: cero timers, cero auto-cierre. Click en la pill alterna, click en
  // cualquier otro sitio cierra, click en la tarjeta no hace nada. La ventana
  // es pantalla completa DESDE QUE NACE (colapsada solo enmascara la franja):
  // sin cambios de geometría no hay efecto chicle entre monitores.

  // ------------------------------------------------------------ superficie
  PanelWindow {
    id: panel
    // Pill ambiental: siempre visible, no reserva espacio (las ventanas no se
    // mueven) y no toma foco de teclado — equivale al NSPanel non-activating.
    anchors { top: true; bottom: true; left: true; right: true }
    // Pantalla mantenida por el guardián (null = la que Quickshell elija).
    screen: root.targetScreen
    color: "transparent"
    WlrLayershell.namespace: "synapsync-usage-notch"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore
    // Colapsada solo la franja tiene input (el resto pasa a las apps);
    // abierta, toda la ventana caza el click-afuera. Como la geometría nunca
    // cambia, el compositor no tiene nada que animar ni que mover de pantalla.
    mask: Region { item: root.expanded ? fullBox : stripBox }

    Item {
      id: hitArea
      anchors.fill: parent
      // Cajas solo para la máscara (invisibles, sin input propio).
      Item {
        id: stripBox
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        width: root.pillWidth
        // x en vez de anclas condicionales: asignar undefined a una anchor
        // line no la libera (ambos lados quedan fijos y el width se ignora).
        x: root.isRight ? parent.width - width : 0
      }
      Item { id: fullBox; anchors.fill: parent }

      // Caza-clicks: solo existe abierta; traga el click-afuera y cierra.
      MouseArea {
        anchors.fill: parent
        visible: root.expanded
        onClicked: { console.log("notch: bg clicked -> close"); root.close() }
      }

      // Tarjeta de detalle (a la izquierda de la pill, solo expandida).
      BorderSurface {
        id: card
        visible: root.expanded
        width: root.cardWidth
        height: Math.min(cardColumn.implicitHeight + root.pad * 2,
                         panel.height - Style.space(32))
        anchors.verticalCenter: parent.verticalCenter
        x: root.isRight ? pill.x - width - root.gap : pill.x + pill.width + root.gap
        color: Util.alpha(Color.popups.background, 0.97)
        borderSpec: Border.surfaceSpec("popups", "border",
          Color.popups.border, Math.max(1, Style.space(2)))
        radius: Style.cornerRadius

        MouseArea {
          anchors.fill: parent
          // Traga el click sobre la tarjeta para que no llegue al fondo.
          onClicked: {}
        }

        Flickable {
          anchors { left: parent.left; right: parent.right; top: parent.top;
                    bottom: parent.bottom; margins: root.pad }
          contentWidth: width
          contentHeight: cardColumn.implicitHeight
          clip: true
          boundsBehavior: Flickable.StopAtBounds
          Column {
            id: cardColumn
            width: parent.width
            spacing: Style.space(8)

          Row {
            width: parent.width
            spacing: Style.space(8)
            Text {
              text: root.settingsMode ? "Providers" : "Usage"
              width: parent.width - gearBox.width - parent.spacing
              color: Color.popups.text
              font { family: Style.font.family; pixelSize: Style.font.title; bold: true }
            }
            Item {
              id: gearBox
              width: Style.space(26)
              height: Style.font.title + Style.space(6)
              Text {
                text: "\u2699"
                anchors.centerIn: parent
                color: root.settingsMode ? Color.accent
                  : Util.alpha(Color.popups.text, 0.7)
                font { family: Style.font.family; pixelSize: Style.font.title }
              }
              MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: root.settingsMode = !root.settingsMode
              }
            }
          }

          Column {
            visible: root.settingsMode
            width: parent.width
            spacing: Style.space(2)
            Repeater {
              model: root.providers
              delegate: Item {
                width: parent.width
                height: Style.font.body + Style.font.caption + Style.space(10)
                property bool isOff: (modelData.status || "") === "disabled"
                Row {
                  anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter }
                  spacing: Style.space(8)
                  Rectangle {
                    width: Style.space(10)
                    height: Style.space(10)
                    radius: width / 2
                    anchors.verticalCenter: parent.verticalCenter
                    color: parent.parent.isOff
                      ? Util.alpha(Color.popups.text, 0.25) : "#46a758"
                  }
                  Column {
                    width: parent.width - Style.space(18)
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 1
                    Text {
                      text: modelData.displayName || "?"
                      color: Color.popups.text
                      font { family: Style.font.family; pixelSize: Style.font.body; bold: true }
                      elide: Text.ElideRight
                      width: parent.width
                    }
                    Text {
                      text: root.subLine(modelData)
                      color: Util.alpha(Color.popups.text, 0.6)
                      font { family: Style.font.family; pixelSize: Style.font.caption }
                      elide: Text.ElideRight
                      width: parent.width
                    }
                  }
                }
                MouseArea {
                  anchors.fill: parent
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.toggleProvider(modelData.id)
                }
              }
            }
            Item {
              width: parent.width
              height: Style.font.body + Style.space(10)
              Text {
                anchors { left: parent.left; verticalCenter: parent.verticalCenter }
                text: "Screen"
                color: Color.popups.text
                font { family: Style.font.family; pixelSize: Style.font.body; bold: true }
              }
              Text {
                anchors { right: parent.right; verticalCenter: parent.verticalCenter }
                text: root.placementScreen || "auto"
                color: Util.alpha(Color.popups.text, 0.7)
                font { family: Style.font.family; pixelSize: Style.font.body }
              }
              MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: root.cycleScreen()
              }
            }
            Item {
              width: parent.width
              height: Style.font.body + Style.space(10)
              Text {
                anchors { left: parent.left; verticalCenter: parent.verticalCenter }
                text: "Edge"
                color: Color.popups.text
                font { family: Style.font.family; pixelSize: Style.font.body; bold: true }
              }
              Text {
                anchors { right: parent.right; verticalCenter: parent.verticalCenter }
                text: root.placementEdge === "left" ? "left" : "right"
                color: Util.alpha(Color.popups.text, 0.7)
                font { family: Style.font.family; pixelSize: Style.font.body }
              }
              MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: root.toggleEdge()
              }
            }
            Item {
              width: parent.width
              height: Style.font.body + Style.space(12)
              Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "\u27F3 Refresh now"
                color: Util.alpha(Color.popups.text, 0.8)
                font { family: Style.font.family; pixelSize: Style.font.body }
              }
              MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: root.poll()
              }
            }
          }

          Repeater {
            visible: !root.settingsMode
            model: root.activeProviders
            delegate: Column {
              width: parent.width
              spacing: Style.space(4)
              opacity: (modelData.status || "ok") === "stale" ? 0.6 : 1
              Text {
                text: (modelData.displayName || "?")
                  + (modelData.status === "stale"
                    ? " · stale · " + root.ageCopy(modelData.fetchedAt)
                    : modelData.status === "needsAuth" ? " · sign in"
                    : modelData.status === "error" ? " · error" : "")
                color: Color.popups.text
                font { family: Style.font.family; pixelSize: Style.font.body; bold: true }
                elide: Text.ElideRight
                width: parent.width
              }
              Repeater {
                model: modelData.windows || []
                delegate: Column {
                  width: parent.width
                  spacing: 2
                  Text {
                    text: root.windowLine(modelData)
                    color: Util.alpha(Color.popups.text, 0.85)
                    font { family: Style.font.family; pixelSize: Style.font.caption }
                    elide: Text.ElideRight
                    width: parent.width
                  }
                  Rectangle {
                    width: parent.width
                    height: Math.max(4, Style.space(4))
                    radius: height / 2
                    color: Util.alpha(Color.popups.text, 0.18)
                    Rectangle {
                      width: parent.width * (modelData.usedFraction || 0)
                      height: parent.height
                      radius: parent.radius
                      color: root.bandColor(modelData.usedFraction || 0, false)
                    }
                  }
                }
              }
              Text {
                visible: (!modelData.windows || modelData.windows.length === 0)
                         && !!modelData.error
                text: modelData.error
                color: Util.alpha(Color.popups.text, 0.6)
                font { family: Style.font.family; pixelSize: Style.font.caption }
                wrapMode: Text.Wrap
                width: parent.width
              }
            }
          }

          Text {
            visible: root.activeProviders.length === 0 && !root.settingsMode
            text: root.lastError !== "" ? root.lastError
              : root.providers.length > 0
                ? "All providers off — open \u2699 to enable"
                : "No providers yet — install & sign in to Claude Code or Codex, then run: python3 usage.py doctor"
            color: Util.alpha(Color.popups.text, 0.7)
            font { family: Style.font.family; pixelSize: Style.font.caption }
            wrapMode: Text.Wrap
            width: parent.width
          }
          }
        }
      }

      // La pill: una celda por proveedor (anillo + %).
      Rectangle {
        id: pill
        width: root.pillWidth
        height: root.pillHeight
        anchors.verticalCenter: parent.verticalCenter
        x: root.isRight ? parent.width - width : 0
        // Mitad exterior redondeada, pegada al borde (inverse-rounded del original).
        topLeftRadius: root.isRight ? width / 2 : 0
        bottomLeftRadius: root.isRight ? width / 2 : 0
        topRightRadius: root.isRight ? 0 : width / 2
        bottomRightRadius: root.isRight ? 0 : width / 2
        color: Util.alpha(Color.background, 0.92)
        border {
          width: 1
          color: pillMouse.containsMouse || root.expanded
            ? Color.accent : Util.alpha(Color.popups.border, 0.6)
        }

        MouseArea {
          id: pillMouse
          anchors.fill: parent
          hoverEnabled: true
          cursorShape: Qt.PointingHandCursor
          acceptedButtons: Qt.LeftButton | Qt.RightButton
          onClicked: (mouse) => {
            console.log("notch: pill clicked", mouse.button, "expanded was", root.expanded)
            if (mouse.button === Qt.RightButton) {
              root.open(); root.settingsMode = true; root.poll()
            } else if (root.expanded) root.close()
            else { root.open(); root.poll() }
          }
        }

        Column {
          id: ringColumn
          anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter }
          spacing: Style.space(4)

          Repeater {
            id: ringRepeater
            model: root.activeProviders
            delegate: Item {
              width: ringColumn.width
              height: root.rowH - Style.space(4)
              property alias ring: ringCanvas
              property var head: root.headline(modelData)
              property bool dimmed: (modelData.status || "ok") !== "ok"
              // Los conteos (~6.3M, $61.65) son más largos que un %: van en
              // tipografía menor para no salirse de la pill.
              property bool isCount: {
                var h = head
                return !!h && (h.usedFraction === null
                  || h.usedFraction === undefined)
              }

              Canvas {
                id: ringCanvas
                width: Style.space(24); height: Style.space(24)
                anchors { horizontalCenter: parent.horizontalCenter; top: parent.top }
                onPaint: {
                  var ctx = getContext("2d")
                  ctx.clearRect(0, 0, width, height)
                  var cx = width / 2, cy = height / 2, r = width / 2 - 3
                  ctx.lineWidth = 3
                  ctx.strokeStyle = Qt.rgba(1, 1, 1, 0.15)
                  ctx.beginPath()
                  ctx.arc(cx, cy, r, 0, Math.PI * 2)
                  ctx.stroke()
                  var frac = (parent.head && parent.head.usedFraction) || 0
                  ctx.strokeStyle = root.bandColor(frac, parent.dimmed)
                  ctx.beginPath()
                  ctx.arc(cx, cy, r, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * frac)
                  ctx.stroke()
                }
              }
              Text {
                text: {
                  if (!parent.head) return "–"
                  if (parent.head.usedFraction === null
                      || parent.head.usedFraction === undefined)
                    return root.countText(parent.head)
                  return Math.round(parent.head.usedFraction * 100)
                }
                color: parent.dimmed ? Util.alpha(Color.foreground, 0.45) : Color.foreground
                font {
                  family: Style.font.family
                  pixelSize: parent.isCount
                    ? Math.max(8, Style.font.caption - 3) : Style.font.caption
                  bold: true
                }
                anchors { horizontalCenter: parent.horizontalCenter; top: ringCanvas.bottom; topMargin: -2 }
              }
            }
          }

          Text {
            visible: root.activeProviders.length === 0
            text: "○"
            color: Util.alpha(Color.foreground, 0.4)
            font.pixelSize: Style.font.body
            anchors.horizontalCenter: parent.horizontalCenter
          }
        }
      }
    }
  }
}
