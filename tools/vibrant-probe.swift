// Does the room actually reach the pixels?
//
// The shop's new window recipe (Apple's material under a WebKit view that has stopped painting its
// own background) is worth nothing unless colour from behind the window survives the glass. A
// stylesheet can promise translucency all day, and `cacheDisplay` — the one way a process may image
// its own views — renders the view tree WITHOUT the backdrop the window server puts behind it, so it
// would answer "yes" to a question it is not able to ask. This harness therefore holds a real window
// on the screen over a loud backdrop of its own making, lets the shop photograph that part of the
// screen, and reads colour back out of the photograph. The backdrop is stripes of pure red, green,
// blue and yellow that are nothing like the shop's palette, so strong colour found inside the shop's
// window can only have arrived from behind it.
//
//   vibrant-probe cache <url> <vibrant|plain> <out.png>
//       image the window's OWN view tree and nothing else. This cannot see the desktop behind the
//       window — `cacheDisplay` is drawn without that backdrop — but it is the one question that can
//       be asked without the window server's cooperation at all: does the page survive being laid over
//       the material, or does the material swallow it? A shop page in the picture answers it.
//
//   vibrant-probe show <url> <vibrant|plain> <out.png> [report]
//       put the window up, photograph it from inside, say what reached the pixels
//   vibrant-probe hold <url> <vibrant|plain> <report>
//       put the window up and wait for the shell to photograph it, then stand down on a sentinel file
//   vibrant-probe measure <png> <scale> <titlebarPt> <x> <y> <w> <h>
//       the chroma of one block, addressed in the CSS points the page reports
//
// Two regions answer two different questions. The page band is bare paper where a page paints its own
// and the room where it does not, so its chroma is the proof the material arrived. The panel has to
// keep the room visible and still hold its figures, which is why legibility is read off the same
// photograph rather than believed from the stylesheet.
//
// Why this is a delegate and an application run loop rather than the top-level script it started as:
// a tool that only pumps `RunLoop.main` puts its window on screen, gets the keyboard, and still never
// has its web view displayed — three runs photographed a blank page in BOTH modes while `takeSnapshot`
// of the very same web view rendered the whole dashboard. That blank is the harness, not the glass,
// and it is only the least suspicious thing here, so the harness has to be a real app before any
// number it prints can be believed.
import Cocoa
import WebKit

let args = CommandLine.arguments

/// Written to as the run goes on, so a shell that started this app with `open` can read the findings
/// out of a file — a LaunchServices-launched app has no stdout anyone is listening to.
var reportPath = ""
var lines = [String]()
func say(_ s: String) {
    lines.append(s)
    Swift.print(s)
    if !reportPath.isEmpty {
        try? lines.joined(separator: "\n").write(toFile: reportPath, atomically: true, encoding: .utf8)
    }
}

final class Stripes: NSView {
    override func draw(_ dirty: NSRect) {
        let colours: [NSColor] = [.red, .green, .blue, .yellow, .magenta, .cyan,
                                  .systemOrange, .systemPurple]
        let w = bounds.width / CGFloat(colours.count)
        for (i, c) in colours.enumerated() {
            c.setFill()
            NSRect(x: bounds.minX + CGFloat(i) * w, y: bounds.minY, width: w + 1, height: bounds.height).fill()
        }
    }
}

/// max−min across the channels, sampled on a grid: what a grey shows near nought and a colour 40 and up.
func chroma(_ rep: NSBitmapImageRep, _ x: Int, _ y: Int, _ w: Int, _ h: Int) -> (median: Double, mean: Double, n: Int) {
    var values = [Int]()
    var py = max(0, y)
    while py < min(rep.pixelsHigh, y + h) {
        var px = max(0, x)
        while px < min(rep.pixelsWide, x + w) {
            if let c = rep.colorAt(x: px, y: py) {
                let r = Int((c.redComponent * 255).rounded())
                let g = Int((c.greenComponent * 255).rounded())
                let b = Int((c.blueComponent * 255).rounded())
                values.append(max(r, g, b) - min(r, g, b))
            }
            px += 2
        }
        py += 2
    }
    guard !values.isEmpty else { return (-1, -1, 0) }
    values.sort()
    return (Double(values[values.count / 2]), Double(values.reduce(0, +)) / Double(values.count), values.count)
}

// ---------------------------------------------------------------- the page's own coordinates
//
// Read out of the live document rather than assumed, because the rectangles that matter are wherever
// the shop's layout put them on this run at this width.
let geometryScript = """
(function(){
  var r = function (sel) { var e = document.querySelector(sel); if (!e) return null;
    var b = e.getBoundingClientRect();
    return [Math.round(b.left), Math.round(b.top), Math.round(b.width), Math.round(b.height)]; };
  return JSON.stringify({view: r('#view') || r('.view'), panel: r('#view .stat') || r('#view .card'),
                         tiles: document.querySelectorAll('#view .stat, #view .card').length,
                         dpr: window.devicePixelRatio});
})()
"""

/// The class the shell adds in the real app, put here the same way the shell puts it: before the
/// document exists, so there is no frame painted without it.
let vibrantScript = """
var addVibrant = function () { if (document.documentElement) document.documentElement.classList.add('vibrant'); };
addVibrant();
document.addEventListener('DOMContentLoaded', addVibrant);
"""

final class Probe: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    let size = NSSize(width: 1100, height: 700)
    var mode = ""
    var url = URL(string: "about:blank")!
    var outPath = ""
    var hold = false
    var cacheOnly = false
    var snapshotPath = ""
    var room: NSWindow!
    var window: NSWindow!
    var web: WKWebView!
    var answered: Any? = nil
    var answeredKey = ""

    func applicationDidFinishLaunching(_ note: Notification) {
        // The room, floating just under the shop window. Both are lifted above ordinary windows so the
        // photograph holds the shop and its backdrop and nothing the owner happened to have open.
        let float = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.floatingWindow)))
        room = NSWindow(contentRect: NSRect(x: 120, y: 40, width: size.width, height: size.height),
                        styleMask: .borderless, backing: .buffered, defer: false)
        room.level = NSWindow.Level(rawValue: float.rawValue - 1)
        // Qoder holds this display in full screen, and an ordinary window from another app is put on
        // the desktop's own Space — which is why one run photographed the editor and the widgets and
        // no harness window at all. Both windows are told to join whatever Space is showing.
        room.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        room.contentView = Stripes(frame: NSRect(origin: .zero, size: size))
        room.orderFrontRegardless()

        let config = WKWebViewConfiguration()
        if mode == "vibrant" {
            config.userContentController.addUserScript(WKUserScript(source: vibrantScript, injectionTime: .atDocumentStart,
                                                                    forMainFrameOnly: true))
        }
        web = WKWebView(frame: NSRect(origin: .zero, size: size), configuration: config)
        web.autoresizingMask = [.width, .height]

        window = NSWindow(contentRect: room.frame,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "vibrant-probe"
        window.level = NSWindow.Level(rawValue: float.rawValue + 1)
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        if mode == "vibrant" {
            // Siblings, not parent and child: an NSVisualEffectView renders its subviews as part of the
            // material, which is right for a label and ruinous for a web view.
            let content = NSView(frame: NSRect(origin: .zero, size: size))
            let glass = NSVisualEffectView(frame: content.bounds)
            glass.material = .underWindowBackground
            glass.blendingMode = .behindWindow
            glass.state = .active
            glass.autoresizingMask = [.width, .height]
            web.setValue(false, forKey: "drawsBackground")
            if #available(macOS 12.0, *) { web.underPageBackgroundColor = .clear }
            content.addSubview(glass)
            web.frame = content.bounds
            content.addSubview(web)
            window.contentView = content
        } else {
            window.contentView = web
        }
        // A titled window is taller than the content asked for, so it is pulled back down to sit where
        // it was put. The capture rectangle printed later is the whole window, titlebar included.
        if let screen = NSScreen.main {
            var f = window.frame
            f.origin.y = min(f.origin.y, screen.frame.maxY - f.height - 30)
            window.setFrame(f, display: true)
        }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        web.navigationDelegate = self
        web.load(URLRequest(url: url))
    }

    func webView(_ webView: WKWebView, didFinish n: WKNavigation!) { settle(after: 4) }
    func webView(_ webView: WKWebView, didFail n: WKNavigation!, withError e: Error) { settle(after: 4) }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) { settle(after: 4) }

    func settle(after seconds: TimeInterval) {
        perform(#selector(report), with: nil, afterDelay: seconds)
    }

    func ask(_ script: String, _ key: String) {
        answeredKey = key
        answered = nil
        web.evaluateJavaScript(script) { value, _ in self.answered = value }
        let cutoff = Date().addingTimeInterval(8)
        while answered == nil && Date() < cutoff { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }
        say("\(key): " + (answered.map { $0 is String ? $0 as! String : String(describing: $0) } ?? "nil"))
    }

    @objc func report() {
        say("MODE: " + mode)
        say("URL: " + url.absoluteString)
        say("ACTIVE: \(NSApp.isActive) key=\(window.isKeyWindow) occlusion=\(window.occlusionState.rawValue)")
        // The window server knows about a window whether or not it is on the Space currently being
        // shown, and `screencapture -l<id>` images that one window. Since `-R` refuses every rectangle
        // on this Mac today and a full-screen photograph only ever shows the Space in front, these two
        // numbers are the only coordinates this harness has that do not depend on which Space is up.
        say("WINID: \(window.windowNumber) ROOMID: \(room.windowNumber)")
        ask("document.documentElement.className", "CLASS")
        ask("getComputedStyle(document.body).backgroundColor", "BODY BG")
        ask(geometryScript, "GEOMETRY")
        // A second, independent answer to "did the page paint?", and the only way to see how much of it
        // it leaves unpainted. Taken in both modes on purpose: the plain page is the control that says
        // what a see-through snapshot would look like when it is really just a rendering failure.
        // `takeSnapshot` is WebKit imaging its own render, so it proves the shop's content exists
        // without saying anything about the room behind the window — that half needs a photograph.
        if !snapshotPath.isEmpty {
            var done = false
            web.takeSnapshot(with: nil) { image, _ in
                if let image = image, let tiff = image.tiffRepresentation,
                   let rep = NSBitmapImageRep(data: tiff),
                   let png = rep.representation(using: .png, properties: [:]) {
                    try? png.write(to: URL(fileURLWithPath: self.snapshotPath))
                }
                done = true
            }
            let cut = Date().addingTimeInterval(10)
            while !done && Date() < cut { RunLoop.main.run(until: Date().addingTimeInterval(0.1)) }
            say("SNAPSHOT: " + (FileManager.default.fileExists(atPath: snapshotPath) ? snapshotPath : "none"))
        }
        if cacheOnly { windowImage(); finish(); return }
        photograph()
    }

    /// The window's own view tree, imaged by AppKit. The material is drawn into it and so is the page
    /// laid over it; only the room beyond the window is missing, and that half is already measured.
    func windowImage() {
        guard let cv = window.contentView else { say("CACHE: no content view"); return }
        let b = cv.bounds
        guard let rep = cv.bitmapImageRepForCachingDisplay(in: b) else { say("CACHE: no image rep"); return }
        cv.cacheDisplay(in: b, to: rep)
        guard let png = rep.representation(using: .png, properties: [:]),
              (try? png.write(to: URL(fileURLWithPath: outPath))) != nil else {
            say("CACHE: could not write \(outPath)"); return
        }
        say("CACHE: \(rep.pixelsWide)x\(rep.pixelsHigh) -> \(outPath)")
    }

    /// The whole window, in the coordinates `screencapture` counts: down from the top of the screen.
    func rect() -> String {
        guard let screen = NSScreen.main else { return "" }
        let f = window.frame
        return "\(Int(f.minX)),\(Int(screen.frame.maxY - f.maxY)),\(Int(f.width)),\(Int(f.height))"
    }

    func photograph() {
        guard let screen = NSScreen.main else { say("SHOT: no screen"); finish(); return }
        let f = window.frame
        let rect = rect()
        say(String(format: "FRAME: appkit=%.0f,%.0f %.0fx%.0f screen=%.0f,%.0f %.0fx%.0f",
                   f.minX, f.minY, f.width, f.height, screen.frame.minX, screen.frame.minY,
                   screen.frame.width, screen.frame.height))
        say("CAPTURE: -R" + rect)
        say(String(format: "SCALE: %.2f TITLEBARPT: %.1f", window.backingScaleFactor,
                   f.height - window.contentLayoutRect.height))
        if hold {
            say("READY")
            let stop = reportPath + ".stop"
            let live = Date().addingTimeInterval(90)
            while !FileManager.default.fileExists(atPath: stop) && Date() < live {
                RunLoop.main.run(until: Date().addingTimeInterval(0.15))
            }
            say("DONE")
            finish()
            return
        }
        // Whatever the last run left at this path is deleted first: a failed shutter that reads the
        // numbers off the previous photograph is exactly the kind of proof that has already misled here.
        try? FileManager.default.removeItem(atPath: outPath)
        let shot = Process()
        shot.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        shot.arguments = ["-x", "-R" + rect, outPath]
        let pipe = Pipe()
        shot.standardError = pipe
        do {
            try shot.run()
            shot.waitUntilExit()
        } catch {
            say("SHOT: could not run screencapture — \(error.localizedDescription)")
        }
        say("SHOT: exit=\(shot.terminationStatus) -> \(outPath)")
        if shot.terminationStatus != 0 {
            say("SHOT ERR: " + (String(data: pipe.fileHandleForReading.readDataToEndOfFile(),
                                       encoding: .utf8) ?? "").trimmingCharacters(in: .whitespacesAndNewlines))
            finish()
            return
        }
        readBack()
        finish()
    }

    func readBack() {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: outPath)),
              let rep = NSBitmapImageRep(data: data) else {
            say("IMAGE: nothing to read back from \(outPath)"); return
        }
        let scale = Double(rep.pixelsWide) / Double(window.frame.width)
        let titlebar = (window.frame.height - window.contentLayoutRect.height) * scale
        say("IMAGE: \(rep.pixelsWide)x\(rep.pixelsHigh) scale=\(String(format: "%.2f", scale)) titlebarPx=\(String(format: "%.1f", titlebar))")
        guard let json = (answered as? String).flatMap({ try? JSONSerialization.jsonObject(with: $0.data(using: .utf8)!) }) as? [String: Any] else {
            say("CHROMA: no geometry to address"); return
        }
        let pad = 10
        func report(_ label: String, _ box: [Int]?, band: Bool) {
            guard let b = box else { say("CHROMA \(label): nothing to measure"); return }
            let x = Int(Double(b[0] + pad) * scale)
            // A band is the bare page along the top of the view; a panel is the tile with its rim off.
            let y = Int((Double(b[1]) + (band ? 3 : Double(pad))) * scale + titlebar)
            let w = Int(Double(b[2] - 2 * pad) * scale)
            let h = Int(Double(band ? 14 : max(20, b[3] - 2 * pad)) * scale)
            guard x >= 0, y >= 0, x + w < rep.pixelsWide, y + h < rep.pixelsHigh, w > 20, h > 8 else {
                say("CHROMA \(label): rect off the image (\(x),\(y) \(w)x\(h))")
                return
            }
            let m = chroma(rep, x, y, w, h)
            say(String(format: "CHROMA %@: median=%.1f mean=%.1f n=%d", label, m.median, m.mean, m.n))
        }
        report("page band", json["view"] as? [Int], band: true)
        report("panel", json["panel"] as? [Int], band: false)
    }

    func finish() { NSApp.terminate(nil) }
}

// ---------------------------------------------------------------- entry points

// ---------------------------------------------------------------- what is actually see-through
//
// The one question a locked screen cannot answer is whether the shop's page still paints over the
// material. This answers the other half of it, and answers it with the display off: of the pixels
// WebKit itself rendered, how many carry nothing at all? A page that leaves the bare paper
// transparent is a page that lets whatever sits behind it through; a page that paints an opaque
// background has nothing for the glass to do.
if args.count >= 3, args[1] == "alpha" {
    guard let data = try? Data(contentsOf: URL(fileURLWithPath: args[2])),
          let rep = NSBitmapImageRep(data: data) else {
        print("ALPHA: cannot read \(args[2])"); exit(1)
    }
    var clear = 0, solid = 0, seen = 0, sumA = 0
    var chroma = [Int]()
    var py = 0
    while py < rep.pixelsHigh {
        var px = 0
        while px < rep.pixelsWide {
            if let c = rep.colorAt(x: px, y: py) {
                let a = Int((c.alphaComponent * 255).rounded())
                seen += 1
                sumA += a
                if a == 0 { clear += 1 } else if a >= 250 { solid += 1 }
                if a >= 250 {
                    let r = Int((c.redComponent * 255).rounded())
                    let g = Int((c.greenComponent * 255).rounded())
                    let b = Int((c.blueComponent * 255).rounded())
                    chroma.append(max(r, g, b) - min(r, g, b))
                }
            }
            px += 2
        }
        py += 2
    }
    guard seen > 0 else { print("ALPHA: nothing sampled"); exit(1) }
    chroma.sort()
    let med = chroma.isEmpty ? -1.0 : Double(chroma[chroma.count / 2])
    print(String(format: "ALPHA: %@ %dx%d opaqueRep=%d sampled=%d see-through=%.1f%% solid=%.1f%% meanA=%.3f chromaOfSolid=median %.1f",
                 (args[2] as NSString).lastPathComponent, rep.pixelsWide, rep.pixelsHigh,
                 rep.isOpaque ? 1 : 0, seen, 100.0 * Double(clear) / Double(seen),
                 100.0 * Double(solid) / Double(seen), Double(sumA) / Double(seen), med))
    exit(0)
}

if args.count >= 9, args[1] == "measure" {
    guard let data = try? Data(contentsOf: URL(fileURLWithPath: args[2])),
          let rep = NSBitmapImageRep(data: data) else {
        print("MEASURE: cannot read \(args[2])"); exit(1)
    }
    let scale = Double(args[3])!, titlebar = Double(args[4])!
    let (x, y, w, h) = (Double(args[5])!, Double(args[6])!, Double(args[7])!, Double(args[8])!)
    // A whole-screen photograph addressed from its own top-left, because `screencapture -R` refuses to
    // image a region on this Mac today while `-x` alone works: the window's rectangle is added here
    // instead of being cut out there.
    let ox = args.count > 9 ? Int(Double(args[9])!) : 0
    let oy = args.count > 10 ? Int(Double(args[10])!) : 0
    let m = chroma(rep, ox + Int(x * scale), oy + Int((y + titlebar) * scale), Int(w * scale), Int(h * scale))
    print(String(format: "CHROMA median=%.1f mean=%.1f n=%d over %dx%d pt at %.0f,%.0f",
                 m.median, m.mean, m.n, w, h, x, y))
    exit(0)
}

guard args.count >= 5, ["show", "hold", "cache"].contains(args[1]) else {
    let usage = "usage: vibrant-probe show  <url> <vibrant|plain> <out.png> [report]\n" +
                "       vibrant-probe hold  <url> <vibrant|plain> <report>\n" +
                "       vibrant-probe cache <url> <vibrant|plain> <out.png>\n"
    FileHandle.standardError.write(usage.data(using: .utf8)!)
    exit(2)
}
reportPath = args.count >= 6 ? args[5] : (args[1] == "hold" ? args[4] : "")

let probe = Probe()
probe.mode = args[3]
probe.url = URL(string: args[2])!
probe.outPath = (args[1] == "hold") ? "" : args[4]
// The page-only image keeps its own path in every mode: in `show` the shared path is about to be
// overwritten by the photograph, and in `hold` there is no photograph for this process to make.
probe.snapshotPath = (args[1] == "hold") ? reportPath + ".web.png"
                                         : probe.outPath.replacingOccurrences(of: ".png", with: "-web.png")
probe.hold = args[1] == "hold"
probe.cacheOnly = args[1] == "cache"
NSApplication.shared.setActivationPolicy(.regular)
NSApplication.shared.delegate = probe
NSApplication.shared.run()
