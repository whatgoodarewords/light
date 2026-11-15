// Lamp.app: a lamp icon in the menu bar. Click it, or press Control-Option-Shift-L
// anywhere, for the panel; right-click for scenes, open at login, and quit.
// It only talks to the light helper.
//
//   Lamp.app --show              open the panel at launch (what `light gui` does)
//   Lamp.app --test-dir <dir>    test aid: on SIGUSR1, run the steps in <dir>/steps.txt (see runSteps)
import AppKit
import Carbon.HIToolbox
import Combine
import ServiceManagement
import SwiftUI

final class KeyPanel: NSPanel {
    override var canBecomeKey: Bool { true }  // so the name field for a saved look can take typing
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var panel: KeyPanel!
    private var model: LampModel!
    private var openTimer: Timer?
    private var idleTimer: Timer?
    private var outsideClicks: Any?
    private var keys: Any?
    private var watching = Set<AnyCancellable>()
    private var panelTop: CGFloat = 0
    private var snapshotSignal: DispatchSourceSignal?
    private var testing = false  // while test steps run, a click elsewhere on the Mac does not close the panel
    private var hotKey: EventHotKeyRef?
    private var hotKeyStatus: OSStatus = -1
    private let scenes = ["candlelight", "fire", "sunset-glow", "dusk", "desert", "warm", "romantic", "night-light", "reading"]

    func applicationDidFinishLaunching(_ notification: Notification) {
        let root = Bundle.main.bundleURL.deletingLastPathComponent()
        model = LampModel(helper: Helper(root: root))

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = statusItem.button {
            button.target = self
            button.action = #selector(clicked)
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        updateIcon()
        model.$on.combineLatest(model.$known).receive(on: RunLoop.main).sink { [weak self] _ in self?.updateIcon() }.store(in: &watching)

        panel = KeyPanel(contentRect: NSRect(x: 0, y: 0, width: 316, height: 300), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: true)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .popUpMenu
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient]
        panel.isMovable = false
        let host = NSHostingView(rootView: PanelView(lamp: model, onHeight: { [weak self] h in self?.resize(to: h) }))
        host.autoresizingMask = [.width, .height]
        panel.contentView = host

        // With the panel closed, look in on a running candle or timer without waking the helper.
        idleTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            guard let self = self, !self.panel.isVisible, !self.model.mode.isEmpty || self.model.timer > 0 else { return }
            self.model.peek()
        }

        registerHotKey()

        if let i = CommandLine.arguments.firstIndex(of: "--test-dir"), i + 1 < CommandLine.arguments.count {
            let dir = URL(fileURLWithPath: CommandLine.arguments[i + 1])
            try? "\(hotKeyStatus)\n".write(to: dir.appendingPathComponent("hotkey-status.txt"), atomically: true, encoding: .utf8)
            signal(SIGUSR1, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: SIGUSR1, queue: .main)
            source.setEventHandler { [weak self] in self?.runSteps(in: dir) }
            source.resume()
            snapshotSignal = source
        }
        if CommandLine.arguments.contains("--show") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { self.show() }
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        show()  // `light gui` or opening the app again
        return false
    }

    private func updateIcon() {
        let name = model.known && model.on ? "lightbulb.fill" : "lightbulb"
        let image = NSImage(systemSymbolName: name, accessibilityDescription: "Lamp")
        image?.isTemplate = true
        statusItem.button?.image = image
    }

    // ------------------------------------------------------------- the panel

    /// Control-Option-Shift-L, from anywhere, opens or closes the panel. A system-wide hot key
    /// registered this way needs no Accessibility permission.
    private func registerHotKey() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, userData in
            guard let userData = userData else { return noErr }
            let me = Unmanaged<AppDelegate>.fromOpaque(userData).takeUnretainedValue()
            DispatchQueue.main.async { me.togglePanel() }
            return noErr
        }, 1, &spec, Unmanaged.passUnretained(self).toOpaque(), nil)
        let id = EventHotKeyID(signature: OSType(0x4C41_4D50), id: 1)  // "LAMP"
        hotKeyStatus = RegisterEventHotKey(UInt32(kVK_ANSI_L), UInt32(controlKey | optionKey | shiftKey), id, GetApplicationEventTarget(), 0, &hotKey)
        if hotKeyStatus != noErr {
            model.say("Another app already uses Control-Option-Shift-L, so that shortcut will not open this panel.")
        }
    }

    private func togglePanel() {
        if panel.isVisible { hide() } else { show() }
    }

    @objc private func clicked() {
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp || event?.modifierFlags.contains(.control) == true {
            hide()
            statusItem.menu = contextMenu()
            statusItem.button?.performClick(nil)
            statusItem.menu = nil
        } else {
            togglePanel()
        }
    }

    private func show() {
        guard let buttonWindow = statusItem.button?.window else { return }
        let anchor = buttonWindow.frame
        panelTop = anchor.minY - 6
        let screen = buttonWindow.screen?.visibleFrame ?? NSScreen.main?.visibleFrame ?? .zero
        let width = panel.frame.width
        let x = min(max(anchor.midX - width + 40, screen.minX + 8), screen.maxX - width - 8)
        panel.setFrameOrigin(NSPoint(x: x, y: panelTop - panel.frame.height))
        panel.makeKeyAndOrderFront(nil)
        model.loadLooks()
        model.refresh()
        openTimer?.invalidate()
        openTimer = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: true) { [weak self] _ in self?.model.refresh() }
        if outsideClicks == nil {
            outsideClicks = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
                if self?.testing == false { self?.hide() }
            }
        }
        if keys != nil { return }
        keys = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            if event.keyCode == 53 {  // Esc
                self?.hide()
                return nil
            }
            return event
        }
    }

    private func hide() {
        panel.orderOut(nil)
        openTimer?.invalidate()
        openTimer = nil
        if let m = outsideClicks { NSEvent.removeMonitor(m) }
        if let m = keys { NSEvent.removeMonitor(m) }
        outsideClicks = nil
        keys = nil
    }

    /// The panel grows and shrinks downward: its top edge stays under the menu bar.
    private func resize(to height: CGFloat) {
        guard height > 10, abs(panel.frame.height - height) > 0.5 else { return }
        var frame = panel.frame
        frame.size.height = height
        frame.origin.y = panelTop - height
        panel.setFrame(frame, display: true)
        panel.invalidateShadow()
    }

    // ------------------------------------------------------ right-click menu

    private func contextMenu() -> NSMenu {
        let menu = NSMenu()
        let open = NSMenuItem(title: "Open the panel", action: #selector(openFromMenu), keyEquivalent: "l")
        open.keyEquivalentModifierMask = [.control, .option, .shift]  // shown beside the item, so the shortcut can be found
        open.target = self
        menu.addItem(open)
        menu.addItem(.separator())
        let sceneItem = NSMenuItem(title: "Scenes stored in the bulb", action: nil, keyEquivalent: "")
        let sub = NSMenu()
        for name in scenes {
            let item = NSMenuItem(title: name, action: #selector(pickScene(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = name
            sub.addItem(item)
        }
        sceneItem.submenu = sub
        menu.addItem(sceneItem)
        menu.addItem(.separator())
        let login = NSMenuItem(title: "Open at login", action: #selector(toggleLogin), keyEquivalent: "")
        login.target = self
        login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        menu.addItem(login)
        let quit = NSMenuItem(title: "Quit Lamp", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        return menu
    }

    @objc private func openFromMenu() {
        show()
    }

    @objc private func pickScene(_ sender: NSMenuItem) {
        if let name = sender.representedObject as? String { model.scene(name) }
    }

    @objc private func toggleLogin() {
        do {
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
            }
        } catch {
            model.say("macOS did not change the login setting: \(error.localizedDescription)")
            show()
        }
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    // -------------------------------------------------------------- test aid
    // Only active when launched with --test-dir. It sends real mouse events to the panel's
    // window, so a test goes through the same hit-testing and gestures as a hand on the trackpad.
    // steps.txt, one per line, coordinates in points from the panel's top-left corner:
    //   show | hide | wait <seconds> | snap <file.png> | click <x> <y> | drag <x1> <y1> <x2> <y2>
    //   type <text> | return
    // When the last step has run, an empty file named "done" appears in the folder.

    private func runSteps(in dir: URL) {
        let text = (try? String(contentsOf: dir.appendingPathComponent("steps.txt"), encoding: .utf8)) ?? ""
        var steps = text.split(separator: "\n").map { $0.split(separator: " ").map(String.init) }.filter { !$0.isEmpty }
        testing = true
        func next() {
            guard !steps.isEmpty else {
                testing = false
                FileManager.default.createFile(atPath: dir.appendingPathComponent("done").path, contents: nil)
                return
            }
            let step = steps.removeFirst()
            let n = step.dropFirst().compactMap { Double($0) }
            var pause = 0.15
            switch step[0] {
            case "show": show()
            case "hotkey": togglePanel()  // the same call the shortcut makes
            case "hide": hide()
            case "wait": pause = n.first ?? 1
            case "snap": if step.count > 1 { writeSnapshot(to: dir.appendingPathComponent(step[1]).path) }
            case "type" where step.count > 1:
                for ch in step[1] { key(String(ch), code: 0) }
            case "return": key("\r", code: 36)
            case "click" where n.count == 2:
                mouse(.leftMouseDown, x: n[0], y: n[1])
                mouse(.leftMouseUp, x: n[0], y: n[1])
            case "drag" where n.count == 4:
                mouse(.leftMouseDown, x: n[0], y: n[1])
                for i in 1...8 {
                    let f = Double(i) / 8
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.06 * Double(i)) {
                        self.mouse(.leftMouseDragged, x: n[0] + (n[2] - n[0]) * f, y: n[1] + (n[3] - n[1]) * f)
                    }
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { self.mouse(.leftMouseUp, x: n[2], y: n[3]) }
                pause = 0.8
            default: break
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + pause) { next() }
        }
        next()
    }

    private func mouse(_ type: NSEvent.EventType, x: Double, y: Double) {
        let point = NSPoint(x: x, y: Double(panel.frame.height) - y)
        if let event = NSEvent.mouseEvent(with: type, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                          windowNumber: panel.windowNumber, context: nil, eventNumber: 0, clickCount: 1, pressure: 1) {
            panel.sendEvent(event)
        }
    }

    private func key(_ chars: String, code: UInt16) {
        for type in [NSEvent.EventType.keyDown, .keyUp] {
            if let event = NSEvent.keyEvent(with: type, location: .zero, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                            windowNumber: panel.windowNumber, context: nil, characters: chars,
                                            charactersIgnoringModifiers: chars, isARepeat: false, keyCode: code) {
                panel.sendEvent(event)
            }
        }
    }

    private func writeSnapshot(to path: String) {
        guard let view = panel.contentView, let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
        view.cacheDisplay(in: view.bounds, to: rep)
        try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
