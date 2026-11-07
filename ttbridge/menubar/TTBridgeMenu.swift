// Menu bar companion for things-todoist-bridge.
//
// It is only a remote control: the sync itself runs in the `ttbridge` background
// service. The app polls the local dashboard API (http://127.0.0.1:<port>) and offers
// pause/resume, "sync now" and a shortcut to the dashboard.
//
// Built by `ttbridge menubar install` with: swiftc -O TTBridgeMenu.swift -o TTBridgeMenu

import AppKit

struct Status {
    var online = false
    var paused = false
    var syncing = false
    var lastSync: Double = 0
    var lastError: String? = nil
}

final class Bridge {
    let base: URL

    init(port: Int) { base = URL(string: "http://127.0.0.1:\(port)")! }

    private func request(_ path: String, body: [String: Any]? = nil, timeout: TimeInterval = 5,
                         done: @escaping ([String: Any]?) -> Void) {
        var req = URLRequest(url: base.appendingPathComponent(path), timeoutInterval: timeout)
        if let body = body {
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.setValue("1", forHTTPHeaderField: "X-TTBridge")
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        URLSession.shared.dataTask(with: req) { data, response, _ in
            let ok = (response as? HTTPURLResponse).map { $0.statusCode < 400 } ?? false
            let json = data.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any]
            DispatchQueue.main.async { done(ok ? (json ?? [:]) : nil) }
        }.resume()
    }

    func status(_ done: @escaping (Status) -> Void) {
        request("api/status") { st in
            guard let st = st else { return done(Status()) }
            self.request("api/config") { cfg in
                var s = Status(online: true)
                s.paused = (cfg?["paused"] as? Bool) ?? false
                s.syncing = (st["syncing"] as? Bool) ?? false
                s.lastSync = (st["last_sync"] as? Double) ?? 0
                s.lastError = st["last_error"] as? String
                done(s)
            }
        }
    }

    func setPaused(_ paused: Bool, done: @escaping () -> Void) {
        request("api/config", body: ["paused": paused]) { _ in done() }
    }

    func syncNow(done: @escaping () -> Void) {
        request("api/sync", body: ["wait": 120], timeout: 130) { _ in done() }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()
    private let bridge: Bridge
    private let agentLabel: String
    private var status = Status()
    private var timer: Timer?

    init(port: Int, agentLabel: String) {
        bridge = Bridge(port: port)
        self.agentLabel = agentLabel
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        menu.delegate = self
        item.menu = menu
        render()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in self?.refresh() }
    }

    func menuWillOpen(_ menu: NSMenu) { refresh() }

    private func refresh() {
        bridge.status { [weak self] s in
            self?.status = s
            self?.render()
        }
    }

    private func symbol(_ name: String, _ fallback: String) -> NSImage? {
        let img = NSImage(systemSymbolName: name, accessibilityDescription: fallback)
        img?.isTemplate = true
        return img
    }

    private func render() {
        let s = status
        let (icon, text): (String, String)
        if !s.online { (icon, text) = ("arrow.triangle.2.circlepath.circle", "Bridge is not running") }
        else if s.syncing { (icon, text) = ("arrow.triangle.2.circlepath", "Syncing…") }
        else if s.paused { (icon, text) = ("pause.circle", "Paused") }
        else if s.lastError != nil { (icon, text) = ("exclamationmark.triangle", "Needs attention") }
        else { (icon, text) = ("arrow.triangle.2.circlepath", s.lastSync > 0 ? "In sync · \(ago(s.lastSync))" : "Waiting for the first sync") }

        item.button?.image = symbol(icon, text)
        item.button?.toolTip = "Things ↔ Todoist: \(text)"
        item.button?.appearsDisabled = !s.online || s.paused

        menu.removeAllItems()
        let head = NSMenuItem(title: text, action: nil, keyEquivalent: "")
        head.isEnabled = false
        menu.addItem(head)
        if let err = s.lastError, s.online {
            let e = NSMenuItem(title: String(err.prefix(80)), action: nil, keyEquivalent: "")
            e.isEnabled = false
            menu.addItem(e)
        }
        menu.addItem(.separator())
        if s.online {
            add(s.paused ? "Resume sync" : "Pause sync", #selector(togglePause), "p")
            add("Sync now", #selector(syncNow), "s").isEnabled = !s.syncing && !s.paused
        } else {
            add("Start bridge", #selector(startBridge), "")
        }
        add("Open dashboard", #selector(openDashboard), "d")
        menu.addItem(.separator())
        add("Quit menu bar icon", #selector(quit), "q")
    }

    @discardableResult
    private func add(_ title: String, _ action: Selector, _ key: String) -> NSMenuItem {
        let mi = NSMenuItem(title: title, action: action, keyEquivalent: key)
        mi.target = self
        menu.addItem(mi)
        return mi
    }

    private func ago(_ ts: Double) -> String {
        let s = Int(Date().timeIntervalSince1970 - ts)
        if s < 60 { return "just now" }
        if s < 3600 { return "\(s / 60) min ago" }
        if s < 86400 { return "\(s / 3600) h ago" }
        return "\(s / 86400) d ago"
    }

    @objc private func togglePause() {
        bridge.setPaused(!status.paused) { [weak self] in self?.refresh() }
    }

    @objc private func syncNow() {
        status.syncing = true
        render()
        bridge.syncNow { [weak self] in self?.refresh() }
    }

    @objc private func openDashboard() {
        NSWorkspace.shared.open(bridge.base)
    }

    @objc private func startBridge() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["kickstart", "gui/\(getuid())/\(agentLabel)"]
        try? p.run()
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in self?.refresh() }
    }

    @objc private func quit() { NSApp.terminate(nil) }
}

var port = 8765
var label = "io.github.ttbridge"
var args = CommandLine.arguments.dropFirst().makeIterator()
while let a = args.next() {
    if a == "--port", let v = args.next(), let n = Int(v) { port = n }
    if a == "--agent-label", let v = args.next() { label = v }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)  // menu bar only, no Dock icon
let delegate = AppDelegate(port: port, agentLabel: label)
app.delegate = delegate
app.run()
