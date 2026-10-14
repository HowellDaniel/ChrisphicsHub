// ---------------------------------------------------------------- appearance
// The shop's clock decides the look, and nothing else does. There is no Light, no Dark, no
// "follow the Mac": the counter's word on 2026-10-13 was "Remove Light, Dark or System because By
// clock is already there", so the modes are gone from the shell rather than merely hidden — the
// window chrome, the page, and the pre-paint script in index.html all work to the one rule.
//
// Kept out of main.swift on purpose: the hour rule below is the shop's, and being a plain
// file lets tools/check-app-daylight.py drive all twenty-four hours against it instead of
// trusting a reading of the code.
import AppKit

enum Theme {
    /// The shop's daylight band: light from 08:00 until 17:00, dark outside it. These are the
    /// owner's own hours, not the sign-in window in the book, and they are the same numbers
    /// public/app.js and public/index.html work to. The check tools fail if they ever disagree.
    static let dayStart = 8
    static let dayEnd = 17

    /// Evening, on the hour the machine claims it is.
    static func isEvening(_ date: Date = Date()) -> Bool {
        let hour = Calendar.current.component(.hour, from: date)
        return hour < dayStart || hour >= dayEnd
    }

    /// The window chrome the given hour asks for. Never nil: the shell has no borrowed look anymore.
    static func appearance(at date: Date = Date()) -> NSAppearance {
        NSAppearance(named: isEvening(date) ? .darkAqua : .aqua)!
    }

    static var appearance: NSAppearance { appearance() }

    // The page repaints itself every second, so a shop left open across 17:00 goes dark on its
    // own. The window's chrome is not part of the page and would not, so this watches for the
    // turn. Only the moment it actually changes does anything get assigned — a timer that
    // restated the same appearance every half minute would redraw the window for nothing.
    private static var lastEvening: Bool?
    private static var clockWatch: Timer?

    static func startClockWatch() {
        guard clockWatch == nil else { return }
        clockWatch = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { _ in syncWithClock() }
    }

    /// Returns whether the shell's look had to change. Split out so the rule can be checked
    /// without waiting for the evening to arrive.
    @discardableResult
    static func syncWithClock() -> Bool {
        let evening = isEvening()
        defer { lastEvening = evening }
        guard evening != lastEvening else { return false }
        NSApp.appearance = appearance
        return true
    }
}
