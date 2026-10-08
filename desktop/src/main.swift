// Chrisphics Hub — native macOS shell for CRISPprint Ghana.
// Own window, own Dock icon, own menu bar; the records engine is the same
// stdlib Python server, started on a private loopback port for this app only.

import Cocoa
import Foundation
import IOKit.pwr_mgt
import PDFKit
import ServiceManagement
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
        NotificationCenter.default.post(name: Theme.changedNotification, object: rawValue)
    }

    /// Posted whenever the palette changes, whoever asked for it — the menu, the page, or
    /// Settings ▸ Appearance. One change, three places that show it.
    static let changedNotification = Notification.Name("crispprint-theme-changed")
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

// ---------------------------------------------------------------- the shop's own authority
//
// tools/shop-server.sh keeps the book on one port, under a certificate this Mac issued itself.
// Nothing here is trusted unless the chain ends at that authority file, so a stranger's
// certificate on a port we happen to probe is still refused.

enum ShopTrust {
    static let rootPEM = URL(fileURLWithPath: NSHomeDirectory())
        .appendingPathComponent("Library/Application Support/mkcert/rootCA.pem")

    static let delegate = SessionTrust()

    static func loopback(_ host: String) -> Bool {
        host == "localhost" || host == "::1" || host.hasPrefix("127.")
    }

    static var root: SecCertificate? {
        guard let text = try? String(contentsOf: rootPEM, encoding: .utf8) else { return nil }
        let base64 = text.split(whereSeparator: { $0 == "\n" || $0 == "\r" })
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty && !$0.hasPrefix("-----") }
            .joined()
        guard let der = Data(base64Encoded: base64) else { return nil }
        return SecCertificateCreateWithData(nil, der as CFData)
    }

    // The certificates the shop's address was offered with. Nothing on the protection space hands
    // the chain over directly, so it comes from the trust object itself.
    static func chain(from space: URLProtectionSpace) -> [SecCertificate] {
        guard let trust = space.serverTrust else { return [] }
        return (SecTrustCopyCertificateChain(trust) as? [SecCertificate]) ?? []
    }

    static func accepts(_ chain: [SecCertificate], host: String) -> Bool {
        guard loopback(host), let anchor = root, !chain.isEmpty else { return false }
        var trust: SecTrust?
        let policy = SecPolicyCreateSSL(true, host as CFString)
        guard SecTrustCreateWithCertificates(chain as CFArray, policy, &trust) == errSecSuccess,
              let built = trust else { return false }
        SecTrustSetAnchorCertificates(built, [anchor] as CFArray)
        SecTrustSetAnchorCertificatesOnly(built, true)
        return SecTrustEvaluateWithError(built, nil)
    }

    final class SessionTrust: NSObject, URLSessionDelegate {
        func urlSession(_ session: URLSession,
                        didReceive challenge: URLAuthenticationChallenge,
                        completionHandler: @escaping (URLSession.AuthChallengeDisposition,
                                                      URLCredential?) -> Void) {
            let space = challenge.protectionSpace
            guard space.authenticationMethod == NSURLAuthenticationMethodServerTrust,
                  let serverTrust = space.serverTrust,
                  ShopTrust.accepts(ShopTrust.chain(from: space), host: space.host) else {
                return completionHandler(.performDefaultHandling, nil)
            }
            completionHandler(.useCredential, URLCredential(trust: serverTrust))
        }
    }
}

// ---------------------------------------------------------------- engine

final class Engine {
    static let shared = Engine()

    private var proc: Process?
    private var handle: FileHandle?
    private let stdinLease = Pipe()
    private var stopping = false
    private var attached = false
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

    private func answers(_ base: URL, _ probe: String = "api/bootstrap") -> Bool {
        let req = URLRequest(url: base.appendingPathComponent(probe), timeoutInterval: 2)
        let config = URLSessionConfiguration.ephemeral
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        var ok = false
        let sem = DispatchSemaphore(value: 0)
        let session = URLSession(configuration: config, delegate: ShopTrust.delegate,
                                 delegateQueue: nil)
        session.dataTask(with: req) { _, res, err in
            ok = err == nil && (res as? HTTPURLResponse)?.statusCode == 200
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 2.5)
        session.finishTasksAndInvalidate()
        return ok
    }

    /// The always-on shop server, if this Mac is running one. Opening a second engine over the
    /// same book is the one thing that can spoil the records, so the app defers to it.
    private func shopServer() -> URL? {
        let port = Int(ProcessInfo.processInfo.environment["CHRISPHICS_SHOP_PORT"] ?? "") ?? 8834
        let url = URL(string: "https://127.0.0.1:\(port)/")!
        return answers(url, "healthz") ? url : nil
    }

    private func tail() -> String {
        guard let data = try? Data(contentsOf: Paths.shared.log),
              let text = String(data: data, encoding: .utf8) else { return "" }
        return text.split(separator: "\n").suffix(12).joined(separator: "\n")
    }

    func start(_ completion: @escaping (Result<URL, EngineError>) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            if let live = self.shopServer() {
                self.attached = true
                self.base = live
                return completion(.success(live))
            }
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
        if attached {
            // The shop's own server is not ours to shut down; the window simply lets go of it.
            base = nil
            attached = false
            return
        }
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

/// WebKit insists that a pop-up's web view is built from the very configuration it hands over, so
/// every window in this app answers to one message controller — and a controller raises an
/// Objective-C exception when the same name is added to it twice. Uncaught, that ended the app the
/// moment a second job sheet was opened. So a single handler answers for each name and every
/// message carries the web view that posted it.
final class Bridge: NSObject, WKScriptMessageHandler {
    static let shared = Bridge()
    private static var claimsKey = 0

    /// Which names this controller already answers to. Carried on the controller itself, so it
    /// disappears with it rather than leaving a reused address to look like a live registration.
    private static func claims(_ controller: WKUserContentController) -> Set<String> {
        (objc_getAssociatedObject(controller, &claimsKey) as? Set<String>) ?? []
    }

    static func bind(_ name: String, script: String?, on controller: WKUserContentController) {
        var claimed = claims(controller)
        guard !claimed.contains(name) else { return }
        if let script = script {
            controller.addUserScript(WKUserScript(source: script, injectionTime: .atDocumentStart,
                                                  forMainFrameOnly: true))
        }
        controller.add(shared, name: name)
        claimed.insert(name)
        objc_setAssociatedObject(controller, &claimsKey, claimed, .OBJC_ASSOCIATION_RETAIN)
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let from = message.webView,
              let window = WebWindow.all.first(where: { $0.web === from }) else { return }
        window.receive(message)
    }
}

final class WebWindow: NSWindowController, WKNavigationDelegate, WKUIDelegate, NSWindowDelegate {
    static var all = [WebWindow]()

    enum Kind { case main, sheet }

    let kind: Kind
    private let base: URL
    private var routeWatch: NSKeyValueObservation?
    private(set) var web: WKWebView!

    init(base: URL, kind: Kind, url: URL? = nil, autoLoad: Bool = true,
         configuration: WKWebViewConfiguration? = nil) {
        self.kind = kind
        self.base = base
        // A pop-up must reuse the configuration WebKit handed us, or it refuses the window.
        let config = configuration ?? WKWebViewConfiguration()
        if configuration == nil { config.websiteDataStore = .nonPersistent() }
        config.preferences.javaScriptCanOpenWindowsAutomatically = true
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

        if kind == .sheet {
            Bridge.bind("sheetPrint",
                        script: "window.print = function(){ window.webkit.messageHandlers.sheetPrint.postMessage(1); };",
                        on: config.userContentController)
        } else {
            // This web view keeps no storage between launches, so the shell hands the page
            // the remembered appearance before it paints — without this, a restart would
            // fall back to following the Mac whatever the shop last chose.
            Bridge.bind("theme",
                        script: "try{localStorage.setItem('\(Theme.key)','\(Theme.saved.rawValue)')}catch(e){}",
                        on: config.userContentController)
            Bridge.bind("external", script: nil, on: config.userContentController)
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
        if kind == .main {
            // The page owns its own zoom level nowhere else, so the shell's setting is applied
            // here and the screen the shop last read is remembered for the next opening.
            web.pageZoom = CGFloat(Prefs.zoom)
            routeWatch = web.observe(\.url, options: [.new]) { view, _ in
                guard let fragment = view.url?.fragment, fragment.hasPrefix("/") else { return }
                if Prefs.startRoute == "auto" { Prefs.lastRoute = "#" + fragment }
            }
        }
        WebWindow.all.append(self)
        if autoLoad {
            let target = (kind == .main && url == nil) ? Prefs.startURL(base) : (url ?? base)
            web.load(URLRequest(url: target))
        }
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
        if let host = url.host, (url.scheme == "http" || url.scheme == "https"),
           ShopTrust.loopback(host) {
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

    func webView(_ view: WKWebView, didReceive challenge: URLAuthenticationChallenge,
                 completionHandler: @escaping (URLSession.AuthChallengeDisposition,
                                               URLCredential?) -> Void) {
        let space = challenge.protectionSpace
        guard space.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = space.serverTrust,
              ShopTrust.accepts(ShopTrust.chain(from: space), host: space.host) else {
            return completionHandler(.performDefaultHandling, nil)
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
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

    func receive(_ message: WKScriptMessage) {
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

    func setZoom(_ factor: Double) {
        web.pageZoom = CGFloat(factor)
    }

    // MARK: NSWindowDelegate

    func windowWillClose(_ note: Notification) {
        // Nothing to let go of on the controller: the handler there is the shared bridge, not this
        // window, so closing a sheet frees the sheet instead of leaving it behind a strong handler.
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

// ---------------------------------------------------------------- preferences
//
// Everything here is the shell's own doing — which screen opens, how big the words are, whether
// this Mac is kept awake, whether the app starts at sign-in. The shop's records stay in the book;
// the one exception is the shop's identity, which the book itself keeps (Settings ▸ Profile).

enum Prefs {
    static let d = UserDefaults.standard
    static let startKey = "crispprint-start-route"
    static let lastKey = "crispprint-last-route"
    static let zoomKey = "crispprint-text-size"
    static let awakeKey = "crispprint-keep-awake"

    /// "auto" means go back where the shop last was; any other value is a hash route.
    static var startRoute: String {
        get { d.string(forKey: startKey) ?? "auto" }
        set { d.set(newValue, forKey: startKey) }
    }

    static var lastRoute: String {
        get { d.string(forKey: lastKey) ?? "" }
        set { d.set(newValue, forKey: lastKey) }
    }

    static var zoom: Double {
        get { let z = d.double(forKey: zoomKey); return z == 0 ? 1.0 : z }
        set { d.set(newValue, forKey: zoomKey) }
    }

    static var keepAwake: Bool {
        get { d.bool(forKey: awakeKey) }
        set { d.set(newValue, forKey: awakeKey) }
    }

    /// The address the main window opens on. Nothing asked for means the page picks the dashboard.
    static func startURL(_ base: URL) -> URL {
        let route = startRoute == "auto" ? lastRoute : startRoute
        guard route.hasPrefix("#/") else { return base }
        return URL(string: base.absoluteString + route) ?? base
    }
}

/// A word to the Mac not to idle away. A phone on the shop Wi-Fi can only reach the book while
/// this computer is awake, and a sleeping Mac looks like a broken shop.
final class Awake {
    static let shared = Awake()
    private var claim: IOPMAssertionID = 0
    var on: Bool { claim != 0 }

    func set(_ want: Bool) {
        if want, claim == 0 {
            var id = IOPMAssertionID(0)
            let why = "\(kAppName) keeps the shop's records reachable" as CFString
            if IOPMAssertionCreateWithName(kIOPMAssertionTypePreventUserIdleSystemSleep as CFString,
                                           IOPMAssertionLevel(kIOPMAssertionLevelOn), why, &id) == kIOReturnSuccess {
                claim = id
            }
        } else if !want, claim != 0 {
            IOPMAssertionRelease(claim)
            claim = 0
        }
        Prefs.keepAwake = on
    }
}

enum LoginItem {
    static var supported: Bool { true }

    static var on: Bool {
        if #available(macOS 13.0, *) { return SMAppService.mainApp.status == .enabled }
        return false
    }

    /// nil means it worked; anything else is the words to show the shop.
    static func set(_ want: Bool) -> String? {
        guard #available(macOS 13.0, *) else {
            return "This macOS version is too old to switch that here. Turn it on under "
                + "System Settings ▸ General ▸ Login Items."
        }
        do {
            if want { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            return nil
        } catch {
            return error.localizedDescription
        }
    }
}

/// The engine the window is already talking to, asked from the shell side. Loopback is the shop's
/// own computer, so these calls carry no sign-in — the same rule the page relies on.
enum ShopAPI {
    struct Refused: Error { let why: String }

    static func call(_ method: String, _ path: String, _ payload: [String: Any]? = nil,
                     _ done: @escaping (Result<[String: Any], Refused>) -> Void) {
        guard let base = Engine.shared.base else {
            return done(.failure(Refused(why: "The records engine is not open.")))
        }
        var request = URLRequest(url: base.appendingPathComponent(path), timeoutInterval: 8)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let payload = payload {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        }
        let session = URLSession(configuration: .ephemeral, delegate: ShopTrust.delegate,
                                 delegateQueue: nil)
        session.dataTask(with: request) { data, response, error in
            session.finishTasksAndInvalidate()
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            let json = (try? JSONSerialization.jsonObject(with: data ?? Data())) as? [String: Any]
            DispatchQueue.main.async {
                if error == nil, (200..<300).contains(status), let json = json {
                    done(.success(json))
                } else if let refused = json?["error"] as? String {
                    done(.failure(Refused(why: refused)))
                } else {
                    done(.failure(Refused(why: error?.localizedDescription
                                          ?? "The engine answered in a way this app could not read (\(status)).")))
                }
            }
        }.resume()
    }
}

enum Shell {
    static var main: WebWindow? { WebWindow.all.first { $0.kind == .main } }

    static func route(_ hash: String) { main?.run("window.location.hash='\(hash)'") }
    static func applyTheme(_ theme: Theme) { main?.run("window.chrisphics.theme('\(theme.rawValue)')") }
    static func applyZoom() { WebWindow.all.forEach { $0.setZoom(Prefs.zoom) } }
}

// ---------------------------------------------------------------- the settings window

private func rowLabel(_ text: String) -> NSTextField {
    let label = NSTextField(labelWithString: text)
    label.font = .systemFont(ofSize: 12)
    label.alignment = .right
    return label
}

private func valueLabel(_ text: String) -> NSTextField {
    let label = NSTextField(labelWithString: text)
    label.font = .systemFont(ofSize: 12)
    label.textColor = .secondaryLabelColor
    label.lineBreakMode = .byTruncatingMiddle
    return label
}

private func noteLabel(_ text: String) -> NSTextField {
    let label = NSTextField(wrappingLabelWithString: text)
    label.font = .systemFont(ofSize: 11)
    label.textColor = .secondaryLabelColor
    label.isSelectable = false
    label.preferredMaxLayoutWidth = 430
    return label
}

private func entry(_ text: String, width: CGFloat = 300) -> NSTextField {
    let field = NSTextField(string: text)
    field.font = .systemFont(ofSize: 12)
    field.widthAnchor.constraint(equalToConstant: width).isActive = true
    return field
}

private func pushButton(_ title: String, _ target: AnyObject, _ action: Selector) -> NSButton {
    let button = NSButton(title: title, target: target, action: action)
    button.bezelStyle = .rounded
    button.font = .systemFont(ofSize: 12)
    return button
}

/// Two columns — the name of the setting on the right of the first, the control on the left of
/// the second — with any explanation stretched underneath both.
private func form(_ pairs: [(String, NSView)], notes: [NSView] = []) -> NSGridView {
    let grid = NSGridView(views: pairs.map { [rowLabel($0.0), $0.1] as [NSView] })
    grid.rowSpacing = 10
    grid.columnSpacing = 12
    grid.translatesAutoresizingMaskIntoConstraints = false
    if grid.numberOfColumns > 1 {
        grid.column(at: 0).xPlacement = .trailing
        grid.column(at: 1).xPlacement = .leading
    }
    for note in notes {
        let row = grid.addRow(with: [note, NSGridCell.emptyContentView])
        row.mergeCells(in: NSRange(location: 0, length: grid.numberOfColumns))
        row.cell(at: 0).xPlacement = .leading
        row.topPadding = 4
    }
    return grid
}

class Pane: NSViewController {
    var paneTitle: String { "General" }
    var symbol: String { "gearshape" }

    /// The pane's own height and width come from the form it holds, which is what a preference
    /// window sizes itself to.
    func place(_ grid: NSGridView) {
        let root = NSView()
        root.addSubview(grid)
        NSLayoutConstraint.activate([
            grid.topAnchor.constraint(equalTo: root.topAnchor, constant: 24),
            grid.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -24),
            grid.centerXAnchor.constraint(equalTo: root.centerXAnchor),
            grid.leadingAnchor.constraint(greaterThanOrEqualTo: root.leadingAnchor, constant: 20),
            root.widthAnchor.constraint(greaterThanOrEqualToConstant: 520),
        ])
        view = root
    }

    /// The one line each pane has for telling the shop what just happened.
    func say(_ text: String, _ color: NSColor = .secondaryLabelColor) {
        statusLabel?.stringValue = text
        statusLabel?.textColor = color
    }

    // Held strongly: the grid keeps it as a subview, but a pane is built before it is placed,
    // and a weak reference would let the line go away between those two moments.
    var statusLabel: NSTextField?
}

final class GeneralPane: Pane {
    override var paneTitle: String { "General" }
    override var symbol: String { "gearshape" }

    private var startPop: NSPopUpButton!
    private var zoomValue: NSTextField!
    private var engineValue: NSTextField!
    private var loginCheck: NSButton!

    private let routes: [(String, String)] = [
        ("auto", "Where I left off"),
        ("#/dashboard", "Dashboard"),
        ("#/jobs", "Print jobs"),
        ("#/spoiled", "Spoiled work"),
        ("#/sync", "Pending sync"),
        ("#/clients", "Clients"),
        ("#/leads", "Enquiries"),
        ("#/accounts", "Accounts"),
        ("#/expenses", "Expenses"),
        ("#/reports", "Reports"),
        ("#/shop", "Shop & devices"),
    ]

    override func viewDidLoad() {
        super.viewDidLoad()

        loginCheck = NSButton(checkboxWithTitle: "Open \(kAppName) when I sign in to this Mac",
                              target: self, action: #selector(toggleLogin))
        loginCheck.state = LoginItem.on ? .on : .off
        loginCheck.isEnabled = LoginItem.supported

        startPop = NSPopUpButton(frame: .zero, pullsDown: false)
        startPop.addItems(withTitles: routes.map { $0.1 })
        startPop.selectItem(at: max(0, routes.firstIndex { $0.0 == Prefs.startRoute } ?? 0))
        startPop.target = self
        startPop.action = #selector(pickStart)

        let slider = NSSlider(value: Prefs.zoom, minValue: 0.9, maxValue: 1.4,
                              target: self, action: #selector(moveZoom))
        slider.widthAnchor.constraint(equalToConstant: 200).isActive = true
        zoomValue = NSTextField(labelWithString: percent(Prefs.zoom))
        zoomValue.font = .systemFont(ofSize: 12)
        zoomValue.widthAnchor.constraint(equalToConstant: 44).isActive = true
        let zoomRow = NSStackView(views: [slider, zoomValue])
        zoomRow.orientation = .horizontal
        zoomRow.spacing = 10

        let awake = NSButton(checkboxWithTitle: "Keep this Mac awake while the shop is open",
                             target: self, action: #selector(toggleAwake))
        awake.state = Awake.shared.on ? .on : .off

        engineValue = valueLabel(Engine.shared.base?.absoluteString ?? "not open yet")
        let status = pushButton("Refresh", self, #selector(refreshEngine))
        engineValue.widthAnchor.constraint(equalToConstant: 260).isActive = true
        let engineRow = NSStackView(views: [engineValue, status])
        engineRow.orientation = .horizontal
        engineRow.spacing = 8

        let bookValue = valueLabel(Paths.shared.db.path)
        bookValue.widthAnchor.constraint(equalToConstant: 300).isActive = true
        let backupValue = valueLabel(Paths.shared.backups.path)
        backupValue.widthAnchor.constraint(equalToConstant: 300).isActive = true

        let dataRow = NSStackView(views: [pushButton("Back up now", self, #selector(backupNow)),
                                          pushButton("Show book in Finder", self, #selector(revealDB)),
                                          pushButton("Open backups", self, #selector(openBackups))])
        dataRow.orientation = .horizontal
        dataRow.spacing = 8

        let notice = noteLabel("")
        statusLabel = notice
        place(form([("Sign-in:", loginCheck),
                    ("Open this screen first:", startPop),
                    ("Text size:", zoomRow),
                    ("Power:", awake),
                    ("Records engine:", engineRow),
                    ("Book:", bookValue),
                    ("Backups:", backupValue),
                    ("", dataRow)],
                   notes: [noteLabel("The book is the one file holding every job, client and payment. "
                                        + "A copy is made to the backups folder each night by itself."),
                           notice]))
    }

    private func percent(_ value: Double) -> String { "\(Int((value * 100).rounded()))%" }

    @objc private func toggleLogin(_ sender: NSButton) {
        let want = sender.state == .on
        if let why = LoginItem.set(want) {
            sender.state = LoginItem.on ? .on : .off
            say(why, .systemRed)
        } else {
            say(want ? "The app will open when this Mac is signed in." : "The app will not open by itself.")
        }
    }

    @objc private func pickStart(_ sender: NSPopUpButton) {
        guard let hit = routes.indices.first(where: { $0 == sender.indexOfSelectedItem }) else { return }
        Prefs.startRoute = routes[hit].0
        if routes[hit].0 != "auto" { Shell.route(routes[hit].0) }
        say("Next time \(kAppName) opens, it will show \(routes[hit].1).")
    }

    @objc private func moveZoom(_ sender: NSSlider) {
        Prefs.zoom = sender.doubleValue
        zoomValue.stringValue = percent(sender.doubleValue)
        Shell.applyZoom()
    }

    @objc private func toggleAwake(_ sender: NSButton) {
        Awake.shared.set(sender.state == .on)
        sender.state = Awake.shared.on ? .on : .off
        say(Awake.shared.on ? "This Mac will not sleep while the shop is open."
                            : "This Mac may sleep when it is left alone.")
    }

    @objc private func refreshEngine() {
        engineValue.stringValue = Engine.shared.base?.absoluteString ?? "not open yet"
    }

    @objc private func backupNow() {
        guard let base = Engine.shared.base else { return say("No engine is open to back up.", .systemRed) }
        Saver.save(base.appendingPathComponent("api/backup"), fallbackName: "chrisphics-backup.db",
                   in: Shell.main?.web.window)
    }

    @objc private func revealDB() {
        let fm = FileManager.default
        NSWorkspace.shared.activateFileViewerSelecting(
            [fm.fileExists(atPath: Paths.shared.db.path) ? Paths.shared.db : Paths.shared.support])
    }

    @objc private func openBackups() { NSWorkspace.shared.open(Paths.shared.backups) }
}

final class AppearancePane: Pane {
    override var paneTitle: String { "Appearance" }
    override var symbol: String { "circle.lefthalf.filled" }

    private var radios = [NSButton]()

    override func viewDidLoad() {
        super.viewDidLoad()
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        for theme in [Theme.light, .dark, .system] {
            let radio = NSButton(radioButtonWithTitle: theme.label, target: self, action: #selector(pick(_:)))
            radio.identifier = NSUserInterfaceItemIdentifier(theme.rawValue)
            radio.state = theme == Theme.saved ? .on : .off
            radios.append(radio)
            stack.addArrangedSubview(radio)
        }
        statusLabel = noteLabel("")
        place(form([("Mode:", stack)],
                   notes: [noteLabel("Light and Dark are the shop's own palettes. Follow the Mac copies "
                                        + "whatever System Settings ▸ Appearance is set to, and changes with it."),
                           statusLabel!]))
        NotificationCenter.default.addObserver(self, selector: #selector(refresh(_:)),
                                               name: Theme.changedNotification, object: nil)
    }

    @objc private func pick(_ sender: NSButton) {
        guard let raw = sender.identifier?.rawValue, let theme = Theme(rawValue: raw) else { return }
        Shell.applyTheme(theme)
    }

    @objc private func refresh(_ note: Notification) {
        let raw = (note.object as? String) ?? Theme.saved.rawValue
        for radio in radios { radio.state = radio.identifier?.rawValue == raw ? .on : .off }
    }

    deinit { NotificationCenter.default.removeObserver(self) }
}

final class ProfilePane: Pane {
    override var paneTitle: String { "Profile" }
    override var symbol: String { "person.crop.circle" }

    private var name: NSTextField!
    private var tagline: NSTextField!
    private var phone: NSTextField!
    private var address: NSTextField!
    private var momo: NSTextField!
    private var install: NSTextField!
    private var currency: NSTextField!
    private var saved: [String: String] = [:]

    override func viewDidLoad() {
        super.viewDidLoad()
        name = entry("")
        tagline = entry("")
        phone = entry("")
        address = entry("")
        momo = valueLabel("—")
        install = valueLabel("—")
        currency = valueLabel("—")
        install.widthAnchor.constraint(equalToConstant: 300).isActive = true

        let buttons = NSStackView(views: [pushButton("Save", self, #selector(save)),
                                          pushButton("Load again", self, #selector(load)),
                                          pushButton("Sign-in and devices…", self, #selector(goShop))])
        buttons.orientation = .horizontal
        buttons.spacing = 8

        statusLabel = noteLabel("These are the words that go on a job sheet and to a client.")
        place(form([("Shop name:", name),
                    ("Tagline:", tagline),
                    ("Phone:", phone),
                    ("Address:", address),
                    ("MoMo number:", momo),
                    ("Devices install from:", install),
                    ("Currency:", currency),
                    ("", buttons)],
                   notes: [statusLabel!, noteLabel("Kept in the book, so every device and every printed sheet "
                                                      + "sees the same shop. Only this Mac can change them.")]))
    }

    override func viewDidAppear() {
        super.viewDidAppear()
        load()
    }

    @objc private func load() {
        ShopAPI.call("GET", "api/shop") { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .failure(let refused): self.say(refused.why, .systemRed)
            case .success(let json):
                let shop = json["shop"] as? [String: Any] ?? [:]
                self.saved = ["name": shop["name"] as? String ?? "",
                              "tagline": shop["tagline"] as? String ?? "",
                              "phone": shop["phone"] as? String ?? "",
                              "address": shop["address"] as? String ?? ""]
                self.name.stringValue = self.saved["name"] ?? ""
                self.tagline.stringValue = self.saved["tagline"] ?? ""
                self.phone.stringValue = self.saved["phone"] ?? ""
                self.address.stringValue = self.saved["address"] ?? ""
                self.momo.stringValue = json["momo_number"] as? String ?? "not set"
                self.install.stringValue = json["address"] as? String ?? "—"
                self.currency.stringValue = "\(shop["currency_symbol"] as? String ?? "") "
                    + "\(shop["currency"] as? String ?? "")"
                self.say("Loaded from the book.")
            }
        }
    }

    @objc private func save() {
        let body: [String: Any] = ["name": name.stringValue, "tagline": tagline.stringValue,
                                   "phone": phone.stringValue, "address": address.stringValue]
        say("Saving…")
        ShopAPI.call("PUT", "api/shop", body) { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .failure(let refused): self.say(refused.why, .systemRed)
            case .success(let json):
                let shop = json["shop"] as? [String: Any] ?? [:]
                self.saved = ["name": shop["name"] as? String ?? "",
                              "tagline": shop["tagline"] as? String ?? "",
                              "phone": shop["phone"] as? String ?? "",
                              "address": shop["address"] as? String ?? ""]
                self.say("Saved. New sheets and client messages carry these words from now on.", .systemGreen)
            }
        }
    }

    @objc private func goShop() {
        Shell.route("#/shop")
        Settings.window?.close()
    }
}

final class UpdatesPane: Pane {
    override var paneTitle: String { "Updates" }
    override var symbol: String { "arrow.triangle.2.circlepath" }

    private var result: NSTextField!

    override func viewDidLoad() {
        super.viewDidLoad()
        let info = Bundle.main.infoDictionary
        let version = info?["CFBundleShortVersionString"] as? String ?? "—"
        let commit = info?["ChrisphicsBuildCommit"] as? String ?? "unknown"
        let built = info?["ChrisphicsBuildDate"] as? String ?? "unknown"

        result = noteLabel("This build is \(version) from commit \(commit), made \(built).")
        result.preferredMaxLayoutWidth = 430

        let buttons = NSStackView(views: [pushButton("Check for update", self, #selector(check)),
                                          pushButton("Open the repository", self, #selector(openRepo))])
        buttons.orientation = .horizontal
        buttons.spacing = 8

        place(form([("This build:", valueLabel(version + "  (commit " + commit + ")")),
                    ("Built:", valueLabel(built)),
                    ("Repository:", valueLabel(UpdatesPane.repository)),
                    ("", buttons)],
                   notes: [result, noteLabel("Nothing leaves this Mac unless the Check button is pressed: it asks "
                                                + "GitHub for the newest commit in the shop's own repository and "
                                                + "compares it with this build. Rebuilding is done on this Mac with "
                                                + "desktop/build-app.sh.")]))
    }

    static let repository = "https://github.com/HowellDaniel/ChrisphicsHub"

    @objc private func openRepo() {
        if let url = URL(string: UpdatesPane.repository) { NSWorkspace.shared.open(url) }
    }

    @objc private func check() {
        result.stringValue = "Asking GitHub…"
        guard let url = URL(string: "https://api.github.com/repos/HowellDaniel/ChrisphicsHub/commits?per_page=1")
        else { return result.stringValue = "That address could not be built." }
        var request = URLRequest(url: url, timeoutInterval: 10)
        request.setValue("\(kAppName)/1.1", forHTTPHeaderField: "User-Agent")
        let session = URLSession(configuration: .ephemeral)
        session.dataTask(with: request) { data, response, error in
            session.finishTasksAndInvalidate()
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            let json = (try? JSONSerialization.jsonObject(with: data ?? Data())) as? [[String: Any]]
            DispatchQueue.main.async {
                guard error == nil, status == 200, let head = json?.first,
                      let sha = head["sha"] as? String else {
                    self.result.stringValue = "GitHub could not be reached just now"
                        + "\(error.map { " (\($0.localizedDescription))" } ?? "")."
                    return
                }
                let mine = Bundle.main.infoDictionary?["ChrisphicsBuildCommit"] as? String ?? ""
                let date = ((head["commit"] as? [String: Any])?["committer"] as? [String: Any])?["date"] as? String ?? ""
                let short = String(sha.prefix(7))
                if mine.isEmpty || mine == "unknown" {
                    self.result.stringValue = "This build carries no commit stamp, so it cannot be compared. "
                        + "The newest commit is \(short)."
                } else if sha.hasPrefix(mine) || mine.hasPrefix(short) {
                    self.result.stringValue = "This is the newest build the repository has (\(short))."
                } else {
                    self.result.stringValue = "The repository has moved on: \(short) \(date). "
                        + "Rebuild with desktop/build-app.sh to take it in."
                }
            }
        }.resume()
    }
}

final class SettingsTabs: NSTabViewController {
    private var watch: NSKeyValueObservation?

    override func viewDidLoad() {
        super.viewDidLoad()
        tabStyle = .toolbar
        for pane in [GeneralPane(), AppearancePane(), ProfilePane(), UpdatesPane()] {
            let item = NSTabViewItem(viewController: pane)
            item.label = pane.paneTitle
            item.image = NSImage(systemSymbolName: pane.symbol, accessibilityDescription: pane.paneTitle)
            addTabViewItem(item)
        }
        // Each pane asks for the height its own form needs, so the window never carries dead
        // space below a short pane nor clips a long one.
        watch = observe(\.selectedTabViewItemIndex) { tabs, _ in
            DispatchQueue.main.async { tabs.fit() }
        }
    }

    override func viewDidAppear() {
        super.viewDidAppear()
        fit()
    }

    private func fit() {
        guard let window = view.window, selectedTabViewItemIndex < tabViewItems.count,
              let pane = tabViewItems[selectedTabViewItemIndex].viewController?.view else { return }
        pane.layoutSubtreeIfNeeded()
        let content = NSSize(width: max(pane.fittingSize.width, 520), height: pane.fittingSize.height)
        let frame = window.frameRect(forContentRect: NSRect(origin: .zero, size: content))
        window.setFrame(NSRect(x: window.frame.midX - frame.width / 2,
                               y: window.frame.maxY - frame.height,
                               width: frame.width, height: frame.height),
                        display: true)
    }
}

enum Settings {
    static weak var window: NSWindow?
    private static var controller: NSWindowController?

    static func show() {
        if controller == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 430),
                                  styleMask: [.titled, .closable, .miniaturizable, .resizable],
                                  backing: .buffered, defer: false)
            window.title = "\(kAppName) Settings"
            window.tabbingMode = .disallowed
            window.toolbarStyle = .preference
            window.isReleasedWhenClosed = false
            // The tab controller gives the window its toolbar and resizes it to the pane showing.
            window.contentViewController = SettingsTabs()
            window.center()
            controller = NSWindowController(window: window)
        }
        controller?.showWindow(nil)
        window = controller?.window
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
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
        // A shop that asked to stay open stays open, from the first moment after sign-in.
        if Prefs.keepAwake { Awake.shared.set(true) }
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
            item("Settings…", #selector(openSettings), ","),
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
            item("Shop & Devices", #selector(goShop), "0"),
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

    // Settings lives in the shell, not the page: it controls the window chrome, the palette,
    // whether the Mac sleeps, and what the app does when it is opened.
    @objc func openSettings() { Settings.show() }

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
    @objc func goShop() { go("#/shop") }

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
             #selector(goShop),
             #selector(backupNow): return main != nil
        default: return true
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
