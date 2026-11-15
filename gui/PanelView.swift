// The panel that opens under the menu bar icon.
// Layout and wording follow mockups/2026-11-14-menu-bar-control/index.html.
import SwiftUI

extension Color {
    init(hex: UInt32, opacity: Double = 1) {
        self.init(.sRGB, red: Double((hex >> 16) & 0xFF) / 255, green: Double((hex >> 8) & 0xFF) / 255, blue: Double(hex & 0xFF) / 255, opacity: opacity)
    }
}

private let ink = Color(hex: 0xF4EFE9)
private let soft = Color(hex: 0xA79D93)
private let ctl = Color.white.opacity(0.10)
private let accent = Color(hex: 0xF08A24)
private let panelFill = Color(hex: 0x2C2724, opacity: 0.97)

let warmthLow = 1000.0, warmthHigh = 6500.0, whiteStart = 2700.0

struct HeightKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}

struct PanelView: View {
    @ObservedObject var lamp: LampModel
    var onHeight: (CGFloat) -> Void

    @State private var saving = false
    @State private var saveName = ""
    @State private var pickingSunrise = false
    @State private var sunriseTime = Calendar.current.date(bySettingHour: 7, minute: 0, second: 0, of: Date()) ?? Date()
    @State private var breatheMinutes = 5
    @State private var sunsetMinutes = 30

    private var running: Bool { !lamp.mode.isEmpty && lamp.mode != "pulse" }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let p = lamp.problem {
                Text(p).font(.system(size: 12)).foregroundColor(ink).fixedSize(horizontal: false, vertical: true)
                    .padding(9).frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 9).fill(Color(hex: 0xB3261E, opacity: 0.35)))
                    .padding(.bottom, 12)
            }
            header
            if !lamp.known {
                hint("Asking the lamp…").padding(.top, 2)
            } else if running {
                runningBody
            } else if lamp.on {
                onBody
            } else {
                offBody
            }
            if lamp.known { footer }
        }
        .padding(EdgeInsets(top: 14, leading: 14, bottom: 10, trailing: 14))
        .frame(width: 316, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 13).fill(panelFill))
        .overlay(RoundedRectangle(cornerRadius: 13).strokeBorder(Color.white.opacity(0.13), lineWidth: 1))
        .foregroundColor(ink)
        .font(.system(size: 13))
        .environment(\.colorScheme, .dark)
        .background(GeometryReader { g in Color.clear.preference(key: HeightKey.self, value: g.size.height) })
        .onPreferenceChange(HeightKey.self) { onHeight($0) }
    }

    // ---------------------------------------------------------------- header

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .center) {
                (Text(lamp.known ? (lamp.on ? "On" : "Off") : "Lamp").font(.system(size: 15, weight: .semibold))
                    + Text(headerDetail).font(.system(size: 15)).foregroundColor(soft))
                    .lineLimit(1)
                Spacer(minLength: 8)
                if lamp.known {
                    Switch(on: lamp.on) { lamp.toggle() }
                }
            }
            if lamp.known && !lamp.on && !running {
                hint("Comes back at \(Int(lamp.bright))%, \(lamp.what).")
            }
        }
        .padding(.bottom, 12)
    }

    private var headerDetail: String {
        guard lamp.known, lamp.on else { return "" }
        return "  \(Int(lamp.bright))% · \(running ? lamp.mode : lamp.what)"
    }

    // ---------------------------------------------------------------- bodies

    private var onBody: some View {
        VStack(alignment: .leading, spacing: 0) {
            brightness
            field("Warmth", value: lamp.white > 0 || lamp.mix > 0 || lamp.draggingWarmth ? "\(Int(lamp.warmth))K" : lamp.what) {
                WarmthSlider(kelvin: $lamp.warmth, showKnob: lamp.white > 0 || lamp.mix > 0 || lamp.draggingWarmth,
                             onDrag: lamp.dragWarmth, onEnd: lamp.endWarmth)
            }
            hint("Left of the mark the bulb mixes colours, which is dimmer. Real white starts at 2700K.").padding(.top, 6)
            section("Looks")
            looksGrid
            section("Moving light")
            modeButtons
        }
    }

    private var offBody: some View {
        VStack(alignment: .leading, spacing: 0) {
            section("Turn on as").padding(.top, -10)
            looksGrid
            section("Moving light")
            modeButtons
            hint("Sunrise: deep red to warm white over 20 minutes.").padding(.top, 9)
        }
    }

    private var runningBody: some View {
        VStack(alignment: .leading, spacing: 0) {
            if lamp.mode == "candle" {
                brightness
                hint("Brightness works while the candle runs.").padding(.top, -6)
            }
            section("Moving light").padding(.top, lamp.mode == "candle" ? 0 : -12)
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    (Text(runningTitle.0).fontWeight(.semibold) + Text(runningTitle.1)).lineLimit(1).minimumScaleFactor(0.85)
                    Spacer(minLength: 6)
                    Button(action: lamp.stop) {
                        Text("Stop").fontWeight(.semibold).foregroundColor(Color(hex: 0x241A12))
                            .padding(.horizontal, 12).padding(.vertical, 4)
                            .background(RoundedRectangle(cornerRadius: 7).fill(ink))
                    }.buttonStyle(.plain)
                }
                if lamp.mode == "candle" {
                    Segments(options: [("calm", "Calm"), ("normal", "Normal"), ("lively", "Lively")], selected: lamp.feel) { lamp.tune($0) }
                } else if lamp.mode == "breathe" {
                    Segments(options: [("5", "5 min"), ("10", "10 min"), ("20", "20 min")], selected: String(breatheMinutes)) {
                        breatheMinutes = Int($0) ?? 5
                        lamp.breathe(minutes: breatheMinutes)
                    }
                } else if lamp.mode == "sunset" {
                    Segments(options: [("15", "15 min"), ("30", "30 min"), ("45", "45 min"), ("60", "1 hour")], selected: String(sunsetMinutes)) {
                        sunsetMinutes = Int($0) ?? 30
                        lamp.sunset(minutes: sunsetMinutes)
                    }
                }
            }
            .padding(EdgeInsets(top: 9, leading: 10, bottom: 9, trailing: 10))
            .background(RoundedRectangle(cornerRadius: 10).fill(accent.opacity(0.16)))
            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(accent.opacity(0.45), lineWidth: 1))
            hint(lamp.mode == "breathe" ? "Breathe in while the light rises, out while it falls. Runs from this Mac." : "Runs from this Mac. Stops if the Mac sleeps or leaves the room.").padding(.top, 9)
            section("Looks")
            looksGrid.opacity(0.5)
            hint("Picking a look stops the \(lamp.mode).").padding(.top, 7)
        }
    }

    private var runningTitle: (String, String) {
        switch lamp.mode {
        case "candle":
            return ("Candle", lamp.timer > 0 ? " running, off at \(clock(lamp.timer))" : " running")
        case "breathe":
            return ("Breathe", lamp.modeEnds > 0 ? " until \(clock(lamp.modeEnds))" : " running")
        case "sunset":
            return ("Sunset", lamp.modeEnds > 0 ? ", off at \(clock(lamp.modeEnds))" : " running")
        case "sunrise":
            if lamp.modeStarts > Date().timeIntervalSince1970 {
                return ("Sunrise", " starts \(clock(lamp.modeStarts)), full light \(clock(lamp.modeEnds))")
            }
            return ("Sunrise", lamp.modeEnds > 0 ? ", full light at \(clock(lamp.modeEnds))" : " running")
        case "tour":
            return ("Tour", " running, then back to your look")
        default:
            return (lamp.mode.capitalized, " running")
        }
    }

    // ----------------------------------------------------------------- parts

    private var brightness: some View {
        field("Brightness", value: "\(Int(lamp.bright))%") {
            BrightnessSlider(value: $lamp.bright, onDrag: lamp.dragBright, onEnd: lamp.endBright)
        }
        .padding(.bottom, 12)
    }

    private func field<S: View>(_ label: String, value: String, @ViewBuilder slider: () -> S) -> some View {
        VStack(spacing: 6) {
            HStack {
                Text(label).foregroundColor(soft)
                Spacer()
                Text(value)
            }
            slider()
        }
    }

    private func section(_ title: String) -> some View {
        Text(title).foregroundColor(soft).padding(.top, 14).padding(.bottom, 7)
    }

    private func hint(_ text: String) -> some View {
        Text(text).font(.system(size: 11.5)).foregroundColor(soft).lineSpacing(1).fixedSize(horizontal: false, vertical: true)
    }

    private var looksGrid: some View {
        LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 6), count: 5), spacing: 8) {
            ForEach(lamp.looks) { look in
                let current = lamp.isCurrent(look)
                Button(action: { lamp.pick(look) }) {
                    VStack(spacing: 3) {
                        RoundedRectangle(cornerRadius: 8).fill(swatch(look)).frame(height: 30)
                            .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.white.opacity(0.10), lineWidth: 1))
                            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.white, lineWidth: current ? 2 : 0).padding(-3))
                        Text(look.name).font(.system(size: 11)).lineLimit(1).truncationMode(.tail)
                            .foregroundColor(current ? .white : Color(hex: 0xD9D1C8))
                    }
                }
                .buttonStyle(.plain)
                .help(look.kelvin > 0 ? "\(look.kelvin)K at \(look.bright)%" : "#" + look.rgb.map { String(format: "%02x", $0) }.joined() + " at \(look.bright)%")
                .contextMenu {
                    if look.saved {
                        Button("Forget \(look.name)") { lamp.forget(look.name) }
                    }
                }
            }
        }
    }

    /// The look's colour, dimmed by its brightness the way `light list` shows it.
    private func swatch(_ look: LookItem) -> Color {
        let level = 0.35 + 0.65 * Double(look.bright) / 100
        return Color(.sRGB, red: Double(look.rgb[0]) / 255 * level, green: Double(look.rgb[1]) / 255 * level, blue: Double(look.rgb[2]) / 255 * level, opacity: 1)
    }

    private var modeButtons: some View {
        HStack(spacing: 6) {
            modeButton("Candle") { lamp.candle() }
            modeButton("Breathe") { lamp.breathe(minutes: breatheMinutes) }
            modeButton("Sunset", enabled: lamp.on) { lamp.sunset(minutes: sunsetMinutes) }
            modeButton("Sunrise", strong: !lamp.on) { lamp.sunrise(minutes: 20) }
        }
    }

    private func modeButton(_ title: String, enabled: Bool = true, strong: Bool = false, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).fontWeight(strong ? .semibold : .regular).foregroundColor(strong ? Color(hex: 0x1C120A) : ink)
                .frame(maxWidth: .infinity).padding(.vertical, 7)
                .background(RoundedRectangle(cornerRadius: 8).fill(strong ? accent : ctl))
        }
        .buttonStyle(.plain).disabled(!enabled).opacity(enabled ? 1 : 0.38)
    }

    // ---------------------------------------------------------------- footer

    private var footer: some View {
        VStack(alignment: .leading, spacing: 0) {
            Rectangle().fill(Color.white.opacity(0.13)).frame(height: 1).padding(.top, 13).padding(.bottom, 9)
            if saving {
                HStack(spacing: 8) {
                    TextField("a name for this look", text: $saveName).textFieldStyle(.roundedBorder).onSubmit(commitSave)
                    link("Save", action: commitSave)
                    link("Cancel") { saving = false }
                }
            } else if pickingSunrise {
                HStack(spacing: 8) {
                    Text("Sunrise starts at").foregroundColor(soft)
                    DatePicker("", selection: $sunriseTime, displayedComponents: .hourAndMinute).labelsHidden().datePickerStyle(.stepperField).fixedSize()
                    Spacer(minLength: 0)
                    link("Set") {
                        pickingSunrise = false
                        lamp.sunrise(minutes: 20, wait: secondsUntil(sunriseTime))
                    }
                    link("Cancel") { pickingSunrise = false }
                }
            } else {
                HStack {
                    Menu {
                        ForEach([15, 30, 45, 60, 120], id: \.self) { m in
                            Button(m < 60 ? "Off in \(m) minutes" : (m == 60 ? "Off in 1 hour" : "Off in 2 hours")) { lamp.offIn(minutes: m) }
                        }
                        if lamp.timer > 0 {
                            Divider()
                            Button("No timer") { lamp.cancelTimer() }
                        }
                    } label: {
                        Text(lamp.timer > 0 ? "Off at \(clock(lamp.timer)) · change" : "Off in…")
                    }
                    .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
                    Spacer()
                    if !running {
                        if lamp.on {
                            link("Save this look…") {
                                saveName = ""
                                saving = true
                            }
                        } else {
                            link("Sunrise at…") { pickingSunrise = true }
                        }
                        Spacer()
                    }
                    if lamp.mode != "tour" {
                        link("Tour", action: lamp.tour)
                    }
                }
                .foregroundColor(Color(hex: 0xD9D1C8))
            }
        }
    }

    private func commitSave() {
        let name = saveName.lowercased().trimmingCharacters(in: .whitespaces).replacingOccurrences(of: " ", with: "-")
        guard !name.isEmpty else { return }
        saving = false
        lamp.save(name)
    }

    private func link(_ title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) { Text(title).foregroundColor(Color(hex: 0xD9D1C8)) }.buttonStyle(.plain)
    }

    private func secondsUntil(_ time: Date) -> Double {
        let cal = Calendar.current
        let parts = cal.dateComponents([.hour, .minute], from: time)
        let next = cal.nextDate(after: Date(), matching: parts, matchingPolicy: .nextTime) ?? Date()
        return max(0, next.timeIntervalSinceNow)
    }
}

// ------------------------------------------------------------------ controls

struct Switch: View {
    let on: Bool
    let action: () -> Void
    var body: some View {
        Capsule().fill(on ? accent : ctl).frame(width: 38, height: 22)
            .overlay(Circle().fill(Color.white).frame(width: 18, height: 18).offset(x: on ? 8 : -8))
            .animation(.easeOut(duration: 0.12), value: on)
            .contentShape(Rectangle())
            .onTapGesture(perform: action)
            .accessibilityLabel("Lamp power")
    }
}

struct Segments: View {
    let options: [(String, String)]
    let selected: String
    let pick: (String) -> Void
    var body: some View {
        HStack(spacing: 0) {
            ForEach(options, id: \.0) { key, title in
                Text(title).foregroundColor(key == selected ? .white : soft)
                    .frame(maxWidth: .infinity).padding(.vertical, 4)
                    .background(RoundedRectangle(cornerRadius: 6).fill(key == selected ? Color.white.opacity(0.20) : Color.clear))
                    .contentShape(Rectangle())
                    .onTapGesture { pick(key) }
            }
        }
        .padding(2)
        .background(RoundedRectangle(cornerRadius: 8).fill(ctl))
    }
}

/// Shared drag handling: a capsule track 22 points high with a round white knob.
private struct Track<Background: View>: View {
    let fraction: Double
    let showKnob: Bool
    let background: (CGFloat) -> Background
    let onChange: (Double) -> Void
    let onEnd: () -> Void
    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width
            ZStack(alignment: .leading) {
                background(w)
                if showKnob {
                    Circle().fill(Color.white).frame(width: 20, height: 20)
                        .shadow(color: .black.opacity(0.5), radius: 2, y: 1)
                        .offset(x: min(max(CGFloat(fraction) * w - 10, 1), w - 21))
                }
            }
            .contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 0)
                .onChanged { g in onChange(Double(min(max(g.location.x / w, 0), 1))) }
                .onEnded { _ in onEnd() })
        }
        .frame(height: 22)
    }
}

struct BrightnessSlider: View {
    @Binding var value: Double
    let onDrag: (Double) -> Void
    let onEnd: (Double) -> Void
    var body: some View {
        Track(fraction: (value - 1) / 99, showKnob: true, background: { w in
            ZStack(alignment: .leading) {
                Capsule().fill(ctl)
                Capsule().fill(LinearGradient(colors: [Color(hex: 0x7A4A1C), Color(hex: 0xF0A040)], startPoint: .leading, endPoint: .trailing))
                    .frame(width: max(22, CGFloat((value - 1) / 99) * w))
            }
        }, onChange: { f in
            let v = (1 + f * 99).rounded()
            if v != value {
                value = v
                onDrag(v)
            }
        }, onEnd: { onEnd(value) })
        .accessibilityLabel("Brightness")
    }
}

struct WarmthSlider: View {
    @Binding var kelvin: Double
    let showKnob: Bool
    let onDrag: (Double) -> Void
    let onEnd: (Double) -> Void
    private func frac(_ k: Double) -> Double { (k - warmthLow) / (warmthHigh - warmthLow) }
    var body: some View {
        Track(fraction: frac(kelvin), showKnob: showKnob, background: { w in
            ZStack(alignment: .leading) {
                Capsule().fill(LinearGradient(stops: [
                    .init(color: Color(hex: 0xFF3300), location: 0), .init(color: Color(hex: 0xFF8200), location: 0.145),
                    .init(color: Color(hex: 0xFFAE54), location: 0.31), .init(color: Color(hex: 0xFFD5A1), location: 0.55),
                    .init(color: Color(hex: 0xFFF9FB), location: 1),
                ], startPoint: .leading, endPoint: .trailing))
                Rectangle().fill(Color(hex: 0x140E0A, opacity: 0.75)).frame(width: 2, height: 28)
                    .offset(x: CGFloat(frac(whiteStart)) * w - 1)
            }
        }, onChange: { f in
            let k = ((warmthLow + f * (warmthHigh - warmthLow)) / 100).rounded() * 100
            if k != kelvin || !showKnob {
                kelvin = k
                onDrag(k)
            }
        }, onEnd: { onEnd(kelvin) })
        .accessibilityLabel("Warmth")
    }
}
