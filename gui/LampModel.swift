// What the panel knows about the lamp, and the things it can ask the helper to do.
// All published values are set on the main thread; requests run one at a time on a
// background queue, in the order they were asked.
import Foundation
import SwiftUI

struct LookItem: Identifiable, Equatable {
    let name: String
    let rgb: [Int]
    let kelvin: Int  // 0 for a plain colour
    let bright: Int
    let saved: Bool
    var id: String { name }
}

final class LampModel: ObservableObject {
    @Published var known = false  // false until the lamp has answered once
    @Published var on = false
    @Published var bright: Double = 70
    @Published var rgb: [Int] = [255, 126, 0]
    @Published var white = 0  // Kelvin when the white LEDs are lit
    @Published var mix = 0  // Kelvin when the colour is one of the warm mixes
    @Published var scene = ""
    @Published var mode = ""  // candle, breathe, sunset, sunrise, tour, or empty
    @Published var modeStarts: Double = 0
    @Published var modeEnds: Double = 0
    @Published var feel = "normal"
    @Published var timer: Double = 0  // when the lamp goes off by timer, 0 if none
    @Published var looks: [LookItem] = []
    @Published var problem: String?
    @Published var warmth: Double = 1800  // where the warmth slider sits, in Kelvin
    @Published var loginItem = false

    var draggingBright = false
    var draggingWarmth = false

    private let helper: Helper
    private let queue = DispatchQueue(label: "lamp.requests")
    private let tourQueue = DispatchQueue(label: "lamp.tour")
    private var liveBusy = false
    private var livePending: [String: Any]?
    private var statusBusy = false

    init(helper: Helper) {
        self.helper = helper
    }

    // ------------------------------------------------------------ requests

    private func send(_ req: [String: Any], start: Bool = true, done: (() -> Void)? = nil) {
        let helper = self.helper
        queue.async {
            var reply: [String: Any]?
            var failure: HelperProblem?
            do {
                reply = try helper.request(req, start: start)
            } catch let p as HelperProblem {
                failure = p
            } catch {
                failure = HelperProblem(text: "Something went wrong talking to the helper.", notRunning: false)
            }
            DispatchQueue.main.async {
                if let reply = reply {
                    self.take(reply, for: req)
                } else if let f = failure {
                    if f.notRunning && !start {
                        // No helper means nothing is running or timed any more.
                        self.mode = ""
                        self.timer = 0
                    } else if !f.text.isEmpty {
                        self.say(f.text)
                    }
                }
                done?()
            }
        }
    }

    private func take(_ reply: [String: Any], for req: [String: Any]) {
        let polled = (req["cmd"] as? String) == "status" || (req["cmd"] as? String) == "peek"
        if let ui = reply["ui"] as? [String: Any], !(polled && changeBusy) {
            apply(ui)  // a poll that was already under way must not undo what was just picked
        }
        if let list = reply["looks"] as? [[String: Any]] {
            looks = list.compactMap { d in
                guard let name = d["name"] as? String, let rgb = d["rgb"] as? [Int] else { return nil }
                return LookItem(name: name, rgb: rgb, kelvin: d["kelvin"] as? Int ?? 0, bright: d["bright"] as? Int ?? 50, saved: d["saved"] as? Bool ?? false)
            }
        }
        let cmd = req["cmd"] as? String ?? ""
        let text = reply["text"] as? String ?? ""
        if reply["ok"] as? Bool == true {
            if let range = text.range(of: "\nNote: ") {
                say(String(text[range.upperBound...]))  // a mode ended early; the helper says why, once
            } else if cmd == "status" {
                // The lamp answers again. Leave a message up long enough to read it.
                if Date().timeIntervalSince(problemSince) > 8 { problem = nil }
            } else if cmd != "peek" && cmd != "looks" && cmd != "live" {
                problem = nil
            }
        } else if !text.isEmpty {
            say(text)
        }
    }

    private func apply(_ ui: [String: Any]) {
        let nowKnown = ui["known"] as? Bool ?? false
        mode = ui["mode"] as? String ?? ""
        modeStarts = ui["mode_starts"] as? Double ?? 0
        modeEnds = ui["mode_ends"] as? Double ?? 0
        feel = ui["feel"] as? String ?? "normal"
        timer = ui["timer"] as? Double ?? 0
        guard nowKnown else { return }
        known = true
        on = ui["on"] as? Bool ?? false
        rgb = ui["rgb"] as? [Int] ?? rgb
        white = ui["white"] as? Int ?? 0
        mix = ui["mix"] as? Int ?? 0
        scene = ui["scene"] as? String ?? ""
        if !draggingBright {
            bright = Double(ui["bright"] as? Int ?? Int(bright))
        }
        if !draggingWarmth {
            if white > 0 { warmth = Double(white) } else if mix > 0 { warmth = Double(mix) }
        }
    }

    /// Ask the lamp how it is. Used while the panel is open.
    func refresh() {
        if statusBusy || changeBusy || draggingBright || draggingWarmth { return }
        statusBusy = true
        send(["cmd": "status"]) { self.statusBusy = false }
    }

    /// What the helper already knows, without starting it or touching the lamp.
    func peek() {
        send(["cmd": "peek"], start: false)
    }

    func loadLooks() {
        send(["cmd": "looks"])
    }

    private var problemSince = Date.distantPast

    func say(_ text: String) {
        problem = text
        problemSince = Date()
    }

    // ----------------------------------------------------- sliders, live

    private func live(_ req: [String: Any]) {
        if liveBusy {
            livePending = req  // only the newest position matters
            return
        }
        liveBusy = true
        var r = req
        r["cmd"] = "live"
        let helper = self.helper
        queue.async {
            let reply = try? helper.request(r)
            DispatchQueue.main.async {
                self.liveBusy = false
                if let ui = reply?["ui"] as? [String: Any] { self.apply(ui) }
                if let next = self.livePending {
                    self.livePending = nil
                    self.live(next)
                }
            }
        }
    }

    func dragBright(_ value: Double) {
        draggingBright = true
        live(["bright": Int(value)])
    }

    func endBright(_ value: Double) {
        livePending = nil
        draggingBright = false
        change(["cmd": "look", "bright": Int(value)])
    }

    func dragWarmth(_ kelvin: Double) {
        draggingWarmth = true
        live(["kelvin": Int(kelvin)])
    }

    func endWarmth(_ kelvin: Double) {
        livePending = nil
        draggingWarmth = false
        let k = Int(kelvin)
        white = k >= 2700 ? k : 0  // so the knob and the label stay where the drag ended
        mix = k < 2700 ? k : 0
        scene = ""
        change(["cmd": "look", "kelvin": k])
    }

    // -------------------------------------------------------------- actions

    private var changeBusy = false
    private var changePending: [String: Any]?

    /// A change to the lamp where only the newest matters: while one is under way, a later
    /// one replaces any that is still waiting, so quick clicks do not queue up and play back.
    private func change(_ req: [String: Any]) {
        if changeBusy {
            changePending = req
            return
        }
        changeBusy = true
        send(req) {
            self.changeBusy = false
            if let next = self.changePending {
                self.changePending = nil
                self.change(next)
            }
        }
    }

    func toggle() {
        on.toggle()  // show it at once; the reply corrects it if the lamp disagrees
        change(["cmd": "look", "power": on ? "on" : "off"])
    }

    func pick(_ look: LookItem) {
        // Show the choice at once; the lamp glides there and the reply confirms or corrects it.
        on = true
        scene = ""
        bright = Double(look.bright)
        rgb = look.rgb
        white = look.kelvin >= 2700 ? look.kelvin : 0
        mix = look.kelvin > 0 && look.kelvin < 2700 ? look.kelvin : 0
        if look.kelvin > 0 { warmth = Double(look.kelvin) }
        var req: [String: Any] = ["cmd": "look", "bright": look.bright]
        if look.kelvin > 0 { req["kelvin"] = look.kelvin } else { req["rgb"] = look.rgb }
        change(req)
    }

    func candle() { change(["cmd": "candle", "feel": "normal"]) }
    func tune(_ feel: String) {
        self.feel = feel
        send(["cmd": "candle_tune", "feel": feel])
    }
    func breathe(minutes: Int) { change(["cmd": "breathe", "seconds": minutes * 60]) }
    func sunset(minutes: Int) { change(["cmd": "sunset", "seconds": minutes * 60]) }
    func sunrise(minutes: Int, wait: Double = 0) { change(["cmd": "sunrise", "seconds": minutes * 60, "wait": wait]) }
    func scene(_ name: String) { change(["cmd": "scene", "name": name]) }
    func stop() { change(["cmd": "stop"]) }
    func offIn(minutes: Int) { send(["cmd": "timer", "seconds": minutes * 60]) }
    func cancelTimer() { send(["cmd": "timer", "seconds": 0]) }
    func save(_ name: String) { send(["cmd": "save", "name": name]) { self.loadLooks() } }
    func forget(_ name: String) { send(["cmd": "forget", "name": name]) { self.loadLooks() } }

    func tour() {
        mode = "tour"
        let helper = self.helper
        tourQueue.async {  // its connection stays open until the tour ends, so it gets its own queue
            let reply = try? helper.request(["cmd": "tour"], timeout: 240)
            DispatchQueue.main.async {
                if let reply = reply { self.take(reply, for: ["cmd": "tour"]) }
            }
        }
    }

    // -------------------------------------------------------------- wording

    /// "the 1800K mix", "white 2700K", "scene fire" or "#ff2a00".
    var what: String {
        if !scene.isEmpty { return "scene \(scene)" }
        if white > 0 { return "white \(white)K" }
        if mix > 0 { return "the \(mix)K mix" }
        return "#" + rgb.map { String(format: "%02x", $0) }.joined()
    }

    func isCurrent(_ look: LookItem) -> Bool {
        guard known, on, mode.isEmpty, scene.isEmpty else { return false }
        if look.kelvin > 0 { return white == look.kelvin || (white == 0 && mix == look.kelvin) }
        return white == 0 && rgb == look.rgb
    }
}

func clock(_ epoch: Double) -> String {
    let f = DateFormatter()
    f.dateFormat = "HH:mm"
    return f.string(from: Date(timeIntervalSince1970: epoch))
}
