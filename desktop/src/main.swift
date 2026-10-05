// Chrisphics Hub — native macOS shell for CRISPprint Ghana.
// Own window, own Dock icon, own menu bar; the records engine is the same
// stdlib Python server, started on a private loopback port for this app only.

import Cocoa
import Foundation
import PDFKit
import WebKit

let kAppName = "Chrisphics Hub"
// Never rename this one: it is the folder holding the shop's live book.
let kDataFolder = "Chrisphics Hub"

// ---------------------------------------------------------------- appearance
// The page owns the palette; the shell only follows it — window chrome, menu tick,
// and the value handed to the page before it paints so a restart looks the same.
enum Theme: String {
    case light, dark, system

    static let key = "crispprint-theme"
    static var saved: Theme { Theme(rawValue: UserDefaults.standard.string(forKey: key) ?? "") ?? .system }

    var label: String {
        switch self {
        case .light: return "Light"
        case .dark: return "Dark"
        case .system: return "Follow the Mac"
        }
    }

    var appearance: NSAppearance? {
        switch self {
        case .light: return NSAppearance(named: .aqua)
        case .dark: return NSAppearance(named: .darkAqua)
        case .system: return nil
        }
    }

    /// Remember the choice and put the shell's own chrome in step with it.
    func commit() {
        UserDefaults.standard.set(rawValue, forKey: Theme.key)
        NSApp.appearance = appearance
    }
}

struct EngineError: Error { let message: String }

// ---------------------------------------------------------------- locations

final class Paths {
    static let shared = Paths()

    let appRoot: URL
    let support: URL
    let db: URL
    let backups: URL
    let log: URL

    private init() {
        let fm = FileManager.default
        let cwd = URL(fileURLWithPath: fm.currentDirectoryPath, isDirectory: true)
        let resources = Bundle.main.resourceURL ?? cwd
        var root = resources.appendingPathComponent("app", isDirectory: true)
        if let override = ProcessInfo.processInfo.environment["CHRISPHICS_SRC"],
           fm.fileExists(atPath: override + "/server.py") {
            root = URL(fileURLWithPath: override, isDirectory: true)
        } else if !fm.fileExists(atPath: root.appendingPathComponent("server.py").path) {
            root = cwd
        }
        appRoot = root
        if let override = ProcessInfo.processInfo.environment["CHRISPHICS_SUPPORT"], !override.isEmpty {
            let dir = URL(fileURLWithPath: override, isDirectory: true)
            try? fm.createDirectory(at: dir.appendingPathComponent("Backups"), withIntermediateDirectories: true)
            support = dir
            backups = dir.appendingPathComponent("Backups", isDirectory: true)
            db = dir.appendingPathComponent("chrisphics.db")
            log = dir.appendingPathComponent("engine.log")
            return
        }
        let base = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? fm.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support")
        support = base.appendingPathComponent(kDataFolder, isDirectory: true)
        backups = support.appendingPathComponent("Backups", isDirectory: true)
        db = support.appendingPathComponent("chrisphics.db")
        log = support.appendingPathComponent("engine.log")
        try? fm.createDirectory(at: backups, withIntermediateDirectories: true)
    }
}

// ---------------------------------------------------------------- engine

final class Engine {
    static let shared = Engine()

    private var proc: Process?
    private var handle: FileHandle?
    private let stdinLease = Pipe()
    private var stopping = false
    private(set) var base: URL?
    var onDeath: ((String) -> Void)?

    static func python() -> String? {
        let fm = FileManager.default
        var candidates = ["/usr/bin/python3", "/usr/local/bin/python3", "/opt/homebrew/bin/python3"]
        if let env = ProcessInfo.processInfo.environment["CHRISPHICS_PYTHON"], !env.isEmpty {
            candidates.insert(env, at: 0)
        }
        for c in candidates where fm.isExecutableFile(atPath: c) { return c }
        let out = Pipe()
        let helper = Process()
        helper.executableURL = URL(fileURLWithPath: "/usr/bin/xcrun")
        helper.arguments = ["-f", "python3"]
        helper.standardOutput = out
        helper.standardError = Pipe()
        do { try helper.run(); helper.waitUntilExit() } catch { return nil }
        let path = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return fm.isExecutableFile(atPath: path) ? path : nil
    }

    private func freePort() -> Int {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return 8712 }
        defer { close(fd) }
        var yes: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        addr.sin_port = 0
        let bound = withUnsafePointer(to: &addr) { p in
            p.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else { return 8712 }
        var got = sockaddr_in()
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        let read = withUnsafeMutablePointer(to: &got) { p in
            p.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &len) }
        }
        guard read == 0 else { return 8712 }
        return Int(UInt16(bigEndian: got.sin_port))
    }

    private func answers(_ base: URL) -> Bool {
        let req = URLRequest(url: base.appendingPathComponent("api/bootstrap"), timeoutInterval: 2)
        let config = URLSessionConfiguration.ephemeral
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        var ok = false
        let sem = DispatchSemaphore(value: 0)
        URLSession(configuration: config).dataTask(with: req) { _, res, err in
            ok = err == nil && (res as? HTTPURLResponse)?.statusCode == 200
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 2.5)
        return ok
    }

    private func tail() -> String {
        guard let data = try? Data(contentsOf: Paths.shared.log),
              let text = String(data: data, encoding: .utf8) else { return "" }
        return text.split(separator: "\n").suffix(12).joined(separator: "\n")
    }

    func start(_ completion: @escaping (Result<URL, EngineError>) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            guard let py = Engine.python() else {
                return completion(.failure(EngineError(message: """
                \(kAppName) needs Python 3 to run its records engine, and none was found in \
                /usr/bin, /usr/local/bin or /opt/homebrew/bin.

                Install the free Command Line Tools with:  xcode-select --install
                """)))
            }
            let server = Paths.shared.appRoot.appendingPathComponent("server.py")
            guard FileManager.default.fileExists(atPath: server.path) else {
                return completion(.failure(EngineError(message: "Could not find server.py inside the app bundle.")))
            }
            let port = self.freePort()
            var env = ProcessInfo.processInfo.environment
            env["CHRISPHICS_DB"] = Paths.shared.db.path
            env["CHRISPHICS_BACKUP_DIR"] = Paths.shared.backups.path
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONWARNINGS"] = "ignore"
            env["CHRISPHICS_WATCH_STDIN"] = "1"
            env.removeValue(forKey: "CHRISPHICS_PORT")

            FileManager.default.createFile(atPath: Paths.shared.log.path, contents: nil)
            let handle = try? FileHandle(forWritingTo: Paths.shared.log)
            let out = handle ?? Pipe()
            let p = Process()
            p.executableURL = URL(fileURLWithPath: py)
            p.arguments = [server.path, "--no-browser", "--port", String(port)]
            p.environment = env
            p.currentDirectoryURL = Paths.shared.appRoot
            p.standardOutput = out
            p.standardError = out
            // Holding this pipe's write end open is how the engine knows we died.
            p.standardInput = self.stdinLease
            p.terminationHandler = { finished in
                guard !self.stopping else { return }
                let code = finished.terminationStatus
                DispatchQueue.main.async { self.onDeath?("exit code \(code)") }
            }
            do { try p.run() } catch {
                return completion(.failure(EngineError(message: "The records engine could not start: \(error.localizedDescription)")))
            }
            self.proc = p
            self.handle = handle
            self.stopping = false
            let url = URL(string: "http://127.0.0.1:\(port)/")!
            for _ in 0..<80 {
                if p.isRunning, self.answers(url) {
                    self.base = url
                    return completion(.success(url))
                }
                if !p.isRunning { break }
                usleep(150_000)
            }
            let why = p.isRunning
                ? "The engine is running but did not answer on port \(port)."
                : "The engine stopped before it was ready (code \(p.terminationStatus)).\n\n\(self.tail())"
            self.stop()
            completion(.failure(EngineError(message: why)))
        }
    }

    func stop() {
        stopping = true
        if let p = proc, p.isRunning { p.terminate() }
        proc = nil
        try? stdinLease.fileHandleForWriting.close()
        try? handle?.close()
        handle = nil
    }
}

// ---------------------------------------------------------------- save files

final class Saver {
    static func save(_ url: URL, fallbackName: String, in window: NSWindow?) {
        URLSession.shared.dataTask(with: url) { data, res, err in
            guard let data = data, err == nil else {
                let why = err?.localizedDescription ?? "no response"
                return DispatchQueue.main.async { alert("Could not fetch that file: \(why)", window) }
            }
            let header = (res as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition")
            let name = filename(header) ?? fallbackName
            DispatchQueue.main.async {
                let panel = NSSavePanel()
                panel.nameFieldStringValue = name
                panel.canCreateDirectories = true
                let handle = { (response: NSApplication.ModalResponse) in
                    guard response == .OK, let dest = panel.url else { return }
                    try? data.write(to: dest)
                    NSWorkspace.shared.activateFileViewerSelecting([dest])
                }
                if let window = window { panel.beginSheetModal(for: window, completionHandler: handle) }
                else { panel.begin(completionHandler: handle) }
            }
        }.resume()
    }

    private static func filename(_ header: String?) -> String? {
        guard let header = header,
              let range = header.range(of: "filename=\"", options: .caseInsensitive) else { return nil }
        return String(header[range.upperBound...]).components(separatedBy: "\"").first
    }

    static func alert(_ text: String, _ window: NSWindow?) {
        let a = NSAlert()
        a.messageText = kAppName
        a.informativeText = text
        a.alertStyle = .warning
        if let window = window { a.beginSheetModal(for: window) { _ in } } else { a.runModal() }
    }
}

// ---------------------------------------------------------------- windows

final class WebWindow: NSWindowController, WKNavigationDelegate, WKUIDelegate,
                       WKScriptMessageHandler, NSWindowDelegate {
    static var all = [WebWindow]()

    enum Kind { case main, sheet }

    let kind: Kind
    private let base: URL
    private(set) var web: WKWebView!

    init(base: URL, kind: Kind, url: URL? = nil, autoLoad: Bool = true,
         configuration: WKWebViewConfiguration? = nil) {
        self.kind = kind
        self.base = base
        // A pop-up must reuse the configuration WebKit handed us, or it refuses the window.
        let config = configuration ?? WKWebViewConfiguration()
        if configuration == nil { config.websiteDataStore = .nonPersistent() }
        config.preferences.javaScriptCanOpenWindowsAutomatically = true
        if kind == .sheet {
            config.userContentController.addUserScript(WKUserScript(
                source: "window.print = function(){ window.webkit.messageHandlers.sheetPrint.postMessage(1); };",
                injectionTime: .atDocumentStart, forMainFrameOnly: true))
        } else {
            // This web view keeps no storage between launches, so the shell hands the page
            // the remembered appearance before it paints — without this, a restart would
            // fall back to following the Mac whatever the shop last chose.
            config.userContentController.addUserScript(WKUserScript(
                source: "try{localStorage.setItem('\(Theme.key)','\(Theme.saved.rawValue)')}catch(e){}",
                injectionTime: .atDocumentStart, forMainFrameOnly: true))
        }
        let size = kind == .main ? NSSize(width: 1320, height: 860) : NSSize(width: 820, height: 920)
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable],
                              backing: .buffered, defer: false)
        window.tabbingMode = .disallowed
        window.minSize = NSSize(width: kind == .main ? 940 : 480, height: 500)
        window.title = kAppName
        window.setFrameAutosaveName(kind == .main ? "ChrisphicsMain" : "ChrisphicsSheet")
        if kind != .main { window.title = "Job Sheet" }
        window.center()
        super.init(window: window)

        if kind == .sheet { config.userContentController.add(self, name: "sheetPrint") }
        else {
            config.userContentController.add(self, name: "theme")
            config.userContentController.add(self, name: "external")
        }
        web = WKWebView(frame: window.contentView!.bounds, configuration: config)
        web.autoresizingMask = [.width, .height]
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = (kind == .main)
        if #available(macOS 13.3, *) { web.isInspectable = true }
        window.contentView = web
        window.delegate = self
        window.isReleasedWhenClosed = false
        WebWindow.all.append(self)
        if autoLoad { web.load(URLRequest(url: url ?? base)) }
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    static var frontmost: WebWindow? {
        (NSApp.keyWindow?.windowController as? WebWindow) ?? all.last
    }

    override func showWindow(_ sender: Any?) {
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: WKNavigationDelegate

    func webView(_ view: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.allow) }
        if url.scheme == "http", url.host == "127.0.0.1" {
            if url.path.hasPrefix("/api/export/") || url.path.hasPrefix("/api/backup") {
                decisionHandler(.cancel)
                let name = url.path.hasPrefix("/api/backup") ? "chrisphics-backup.db"
                    : (url.path.components(separatedBy: "/").last ?? "export.csv")
                Saver.save(url, fallbackName: name, in: view.window)
                return
            }
            return decisionHandler(.allow)
        }
        decisionHandler(.cancel)
        if url.scheme == "tel" || url.scheme == "mailto" || url.scheme == "https" || url.scheme == "http" {
            NSWorkspace.shared.open(url)
        }
    }

    func webView(_ view: WKWebView, createWebViewWith config: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        let sheet = WebWindow(base: base, kind: .sheet, autoLoad: false, configuration: config)
        sheet.showWindow(nil)
        return sheet.web
    }

    func webView(_ view: WKWebView, didFinish navigation: WKNavigation!) {
        guard kind == .sheet else { return }
        // view.title stays empty here, so ask the page itself for the job reference.
        view.evaluateJavaScript("document.title") { [weak self] value, _ in
            guard let title = value as? String, !title.isEmpty else { return }
            self?.window?.title = title
        }
    }

    func webView(_ view: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        Saver.alert("This screen could not be loaded: \(error.localizedDescription)", view.window)
    }

    // MARK: JS dialogs

    func webView(_ view: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = kAppName
        a.informativeText = message
        a.addButton(withTitle: "OK")
        a.beginSheetModal(for: view.window!) { _ in completionHandler() }
    }

    func webView(_ view: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = kAppName
        a.informativeText = message
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        a.beginSheetModal(for: view.window!) { completionHandler($0 == .OK) }
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        switch message.name {
        case "sheetPrint":
            printSheet()
        case "theme":
            // The page has already re-painted itself; this only brings the shell with it.
            guard let raw = message.body as? String, let theme = Theme(rawValue: raw) else { return }
            theme.commit()
            (NSApp.delegate as? AppDelegate)?.themeDidPick(theme)
        case "external":
            // A client message is handed to the Mac: WhatsApp or Mail takes it from here and
            // the shop presses send there. Only these destinations are let out, so a stray
            // link on the page can never make the machine open something else.
            guard let raw = message.body as? String, let url = URL(string: raw) else { return }
            let host = (url.host ?? "").lowercased()
            let allowed = url.scheme == "mailto" || url.scheme == "tel"
                || (url.scheme == "https" && (host == "wa.me" || host.hasSuffix(".whatsapp.com")))
            guard allowed else { NSSound.beep(); return }
            DispatchQueue.main.async { NSWorkspace.shared.open(url) }
        default:
            break
        }
    }

    // MARK: job sheet printing

    private func makePDF(_ body: @escaping (Data?) -> Void) {
        guard #available(macOS 11.0, *) else { return body(nil) }
        let finish: (Data?) -> Void = { data in
            self.web.evaluateJavaScript("window.__sheetChrome && window.__sheetChrome()") { _, _ in body(data) }
        }
        // createPDF captures with screen styles, so the sheet's on-screen links would
        // print too. Put them in print media for the length of the capture.
        web.evaluateJavaScript(
            "window.__sheetChrome=(function(){var s=document.createElement('style');" +
            "s.textContent='.noprint{display:none!important}';" +
            "(document.head||document.documentElement).appendChild(s);" +
            "return function(){if(s.parentNode)s.remove();};})();"
        ) { _, _ in
            self.web.createPDF(configuration: WKPDFConfiguration()) { result in
                switch result {
                case .success(let data): finish(data)
                case .failure: finish(nil)
                }
            }
        }
    }

    func printSheet() {
        guard kind == .sheet else {
            return Saver.alert("Open a job sheet first — File ▸ Print works on the job sheet window.", window)
        }
        makePDF { data in
            guard let data = data, let doc = PDFDocument(data: data), doc.pageCount > 0 else {
                return Saver.alert("This Mac could not prepare the job sheet for printing.", self.window)
            }
            let info = (NSPrintInfo.shared.copy() as? NSPrintInfo) ?? NSPrintInfo()
            info.horizontalPagination = .fit
            info.verticalPagination = .automatic
            info.jobDisposition = .spool
            let paper = info.paperSize
            let printable = NSSize(width: max(paper.width - info.leftMargin - info.rightMargin, 120),
                                   height: max(paper.height - info.topMargin - info.bottomMargin, 120))
            let op = NSPrintOperation(view: PDFPrintView(document: doc, printable: printable),
                                      printInfo: info)
            op.showsPrintPanel = true
            op.showsProgressPanel = true
            op.run()
        }
    }

    func exportPDF() {
        guard kind == .sheet else {
            return Saver.alert("Open a job sheet first — Export PDF saves that sheet as a PDF file.", window)
        }
        makePDF { data in
            guard let data = data else {
                return Saver.alert("This Mac could not render the job sheet to PDF.", self.window)
            }
            let raw = self.window?.title ?? ""
            let name = (raw.isEmpty || raw == "Job Sheet") ? "job-sheet" : raw
            let panel = NSSavePanel()
            panel.nameFieldStringValue = name + ".pdf"
            panel.begin { response in
                if response == .OK, let dest = panel.url { try? data.write(to: dest) }
            }
        }
    }

    func run(_ js: String) {
        web.evaluateJavaScript(js) { _, err in
            if let err = err { Saver.alert(err.localizedDescription, self.window) }
        }
    }

    // MARK: NSWindowDelegate

    func windowWillClose(_ note: Notification) {
        if kind == .sheet {
            WebWindow.all.removeAll { $0 === self }
        } else {
            NSApp.terminate(nil)
        }
    }
}

/// One PDF page per printable-height band, stacked so AppKit paginates cleanly.
final class PDFPrintView: NSView {
    private let document: PDFDocument
    private let band: NSSize

    init(document: PDFDocument, printable: NSSize) {
        self.document = document
        let width = printable.width > 0 ? printable.width : 480
        let height = printable.height > 0 ? printable.height : 680
        self.band = NSSize(width: width, height: height)
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: height * CGFloat(max(document.pageCount, 1))))
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    private func bandRect(_ index: Int) -> NSRect {
        let top = bounds.height - CGFloat(index + 1) * band.height
        return NSRect(x: 0, y: top, width: band.width, height: band.height)
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.white.setFill()
        dirtyRect.fill()
        for index in 0..<document.pageCount {
            let rect = bandRect(index)
            guard rect.intersects(dirtyRect), let page = document.page(at: index) else { continue }
            let media = page.bounds(for: .mediaBox)
            guard media.width > 0, media.height > 0 else { continue }
            let scale = min(rect.width / media.width, rect.height / media.height)
            let w = media.width * scale, h = media.height * scale
            let target = NSRect(x: rect.minX + (rect.width - w) / 2,
                                y: rect.minY + (rect.height - h) / 2,
                                width: w, height: h)
            guard let ctx = NSGraphicsContext.current?.cgContext else { continue }
            ctx.saveGState()
            ctx.translateBy(x: target.minX - media.minX * scale, y: target.minY - media.minY * scale)
            ctx.scaleBy(x: scale, y: scale)
            page.draw(with: .mediaBox, to: ctx)
            ctx.restoreGState()
        }
    }
}

// ---------------------------------------------------------------- app delegate

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuItemValidation {
    private var loading: NSWindow?
    private var appearanceMenu: NSMenu?

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.appearance = Theme.saved.appearance
        buildMenu()
        showLoading()
        Engine.shared.onDeath = { why in
            Engine.shared.stop()
            let a = NSAlert()
            a.messageText = "The records engine stopped (\(why))"
            a.informativeText = "Your book is safe in \(Paths.shared.db.path).\n\nReopen \(kAppName) to carry on."
            a.addButton(withTitle: "Quit")
            a.runModal()
            NSApp.terminate(nil)
        }
        Engine.shared.start { result in
            DispatchQueue.main.async {
                self.loading?.close()
                self.loading = nil
                switch result {
                case .success(let url):
                    WebWindow(base: url, kind: .main).showWindow(nil)
                case .failure(let error):
                    let a = NSAlert()
                    a.messageText = "\(kAppName) could not open"
                    a.informativeText = error.message
                    a.addButton(withTitle: "Quit")
                    a.runModal()
                    NSApp.terminate(nil)
                }
            }
        }
    }

    private func showLoading() {
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 340, height: 96),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.title = kAppName
        window.isReleasedWhenClosed = false
        let box = NSView(frame: window.contentView!.bounds)
        let label = NSTextField(labelWithString: "Opening your records…")
        label.font = NSFont.systemFont(ofSize: 13, weight: .medium)
        label.alignment = .center
        label.frame = NSRect(x: 20, y: 48, width: 300, height: 20)
        let bar = NSProgressIndicator(frame: NSRect(x: 110, y: 16, width: 120, height: 20))
        bar.style = .spinning
        bar.startAnimation(nil)
        box.addSubview(label)
        box.addSubview(bar)
        window.contentView = box
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        loading = window
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag, let base = Engine.shared.base { WebWindow(base: base, kind: .main).showWindow(nil) }
        return true
    }

    func applicationWillTerminate(_ note: Notification) { Engine.shared.stop() }

    // MARK: menu

    private func buildMenu() {
        let main = NSMenu()
        main.addItem(submenu(kAppName, [
            item("About \(kAppName)", #selector(about)),
            .separator(),
            item("Hide \(kAppName)", #selector(NSApplication.hide(_:)), "h"),
            .separator(),
            item("Quit \(kAppName)", #selector(NSApplication.terminate(_:)), "q"),
        ]))
        main.addItem(submenu("File", [
            item("New Job", #selector(newJob), "n"),
            item("New Quote", #selector(newQuote), "N", [.command, .shift]),
            item("New Enquiry", #selector(newEnquiry), "i"),
            item("New Client", #selector(newClient), "k"),
            item("Receive Payment", #selector(receivePayment), "r"),
            item("Record Money Out", #selector(newExpense), "e"),
            .separator(),
            item("Print Job Sheet", #selector(printSheet), "p"),
            item("Export Job Sheet as PDF", #selector(exportPDF)),
            .separator(),
            item("Close Window", #selector(NSWindow.performClose(_:)), "w"),
        ]))
        main.addItem(submenu("Edit", [
            item("Undo", Selector(("undo:")), "z"),
            item("Redo", Selector(("redo:")), "Z"),
            .separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"),
            item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"),
            item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ]))
        main.addItem(submenu("View", [
            item("Reload", #selector(reloadPage), "R"),
            .separator(),
            appearanceSubmenu(),
            .separator(),
            item("Dashboard", #selector(goDashboard), "1"),
            item("Print Jobs", #selector(goJobs), "2"),
            item("Clients", #selector(goClients), "3"),
            item("Enquiries", #selector(goLeads), "4"),
            item("Accounts", #selector(goAccounts), "5"),
            item("Expenses", #selector(goExpenses), "6"),
            item("Reports", #selector(goReports), "7"),
            item("Spoiled Work", #selector(goSpoiled), "8"),
            item("Pending Sync", #selector(goSync), "9"),
        ]))
        main.addItem(submenu("Data", [
            item("Back Up Book Now", #selector(backupNow), "b"),
            item("Show Data File in Finder", #selector(revealDB)),
            item("Open Backup Folder", #selector(openBackups)),
        ]))
        let windowMenu = submenu("Window", [
            item("Minimise", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Zoom", #selector(NSWindow.performZoom(_:))),
        ])
        main.addItem(windowMenu)
        NSApp.windowsMenu = windowMenu.submenu
        NSApp.mainMenu = main
    }

    private func item(_ title: String, _ action: Selector?, _ key: String = "",
                      _ mask: NSEvent.ModifierFlags = .command) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
        i.keyEquivalentModifierMask = mask
        if action != nil { i.target = self }
        return i
    }

    private func submenu(_ title: String, _ items: [NSMenuItem]) -> NSMenuItem {
        let parent = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        let menu = NSMenu(title: title)
        items.forEach { menu.addItem($0) }
        parent.submenu = menu
        return parent
    }

    private func appearanceSubmenu() -> NSMenuItem {
        let parent = NSMenuItem(title: "Appearance", action: nil, keyEquivalent: "")
        let menu = NSMenu(title: "Appearance")
        for theme in [Theme.light, .dark, .system] {
            let entry = NSMenuItem(title: theme.label, action: #selector(pickTheme(_:)), keyEquivalent: "")
            entry.target = self
            entry.representedObject = theme.rawValue
            entry.state = theme == Theme.saved ? .on : .off
            menu.addItem(entry)
        }
        parent.submenu = menu
        appearanceMenu = menu
        return parent
    }

    // Going through the page keeps one route to the palette: it repaints and tells the
    // shell back what is now showing, so the tick and the window chrome cannot disagree.
    @objc private func pickTheme(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String, let theme = Theme(rawValue: raw) else { return }
        main?.run("window.chrisphics.theme('\(theme.rawValue)')")
    }

    func themeDidPick(_ theme: Theme) {
        appearanceMenu?.items.forEach { $0.state = $0.representedObject as? String == theme.rawValue ? .on : .off }
    }

    private var main: WebWindow? { WebWindow.all.first { $0.kind == .main } }
    private var sheet: WebWindow? { WebWindow.frontmost.flatMap { $0.kind == .sheet ? $0 : nil } }

    @objc func about() {
        let credits = NSAttributedString(
            string: "Printing records and account book\nData file: \(Paths.shared.db.path)",
            attributes: [.font: NSFont.systemFont(ofSize: 11), .foregroundColor: NSColor.secondaryLabelColor])
        NSApp.orderFrontStandardAboutPanel(options: [.credits: credits])
    }

    // The menu opens the same forms the on-page buttons open (window.chrisphics in app.js).
    private func act(_ name: String) { main?.run("window.chrisphics.act('\(name)')") }
    @objc func newJob() { act("new-job") }
    @objc func newQuote() { act("new-quote") }
    @objc func newEnquiry() { act("new-lead") }
    @objc func newClient() { act("new-client") }
    @objc func newExpense() { act("new-expense") }
    @objc func receivePayment() { act("receive-payment") }
    @objc func reloadPage() { (WebWindow.frontmost ?? main)?.web.reload() }
    @objc func printSheet() { (sheet ?? main)?.printSheet() }
    @objc func exportPDF() { (sheet ?? main)?.exportPDF() }

    private func go(_ route: String) { main?.run("window.location.hash='\(route)'") }
    @objc func goDashboard() { go("#/dashboard") }
    @objc func goJobs() { go("#/jobs") }
    @objc func goClients() { go("#/clients") }
    @objc func goLeads() { go("#/leads") }
    @objc func goAccounts() { go("#/accounts") }
    @objc func goExpenses() { go("#/expenses") }
    @objc func goReports() { go("#/reports") }
    @objc func goSpoiled() { go("#/spoiled") }
    @objc func goSync() { go("#/sync") }

    @objc func backupNow() {
        guard let base = Engine.shared.base, let web = main?.web else { return }
        Saver.save(base.appendingPathComponent("api/backup"), fallbackName: "chrisphics-backup.db", in: web.window)
    }

    @objc func revealDB() {
        let fm = FileManager.default
        let target = fm.fileExists(atPath: Paths.shared.db.path) ? Paths.shared.db : Paths.shared.support
        NSWorkspace.shared.activateFileViewerSelecting([target])
    }

    @objc func openBackups() { NSWorkspace.shared.open(Paths.shared.backups) }

    func validateMenuItem(_ item: NSMenuItem) -> Bool {
        switch item.action {
        case #selector(printSheet), #selector(exportPDF): return sheet != nil
        case #selector(pickTheme(_:)): return main != nil
        case #selector(newJob), #selector(newQuote), #selector(newEnquiry), #selector(newClient),
             #selector(newExpense), #selector(receivePayment),
             #selector(goDashboard), #selector(goJobs), #selector(goClients), #selector(goLeads),
             #selector(goAccounts), #selector(goExpenses), #selector(goReports), #selector(goSpoiled), #selector(goSync),
             #selector(backupNow): return main != nil
        default: return true
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
