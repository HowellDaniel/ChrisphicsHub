// Tools/wk-probe — drive the shipped rendering engine and report what the
// screens actually did, so a broken template shows up here rather than on the
// counter. Same WebKit the .app uses, so what passes here passes in the window.
//
//   swiftc -O tools/wk-probe.swift -o /tmp/wk-probe
//   /tmp/wk-probe http://127.0.0.1:8793 /tmp/shots \
//       "open|/#/jobs" "shot|jobs" "eval|document.querySelector('[data-action=\"new-expense\"]').click()" \
//       "poll|!document.getElementById('modal').hidden" "shot|expense-modal"
//
// Commands, in order:
//   open|<path>   load base+path (absolute http(s) URL also accepted)
//   size|<w>x<h>  resize the window and the web view, so media queries answer for that width
//   poll|<js>     wait until the expression is truthy (8s deadline)
//   eval|<js>     run a statement, ignore the result
//   say|<js>      evaluate and print the returned string
//   shot|<name>   save a PNG of the page
//
// WKPROBE_SCRIPT=<file> injects that JavaScript into every page before it runs, which is how a
// probe of the shop's own DOM is written once as functions instead of pasted into a shell argument.
// WKPROBE_HANDLERS=<names> binds those native script handlers, the way the .app binds "theme",
// before the web view exists — a page decides whether it is inside the Mac app on the first frame,
// so a handler added later would never be seen.
import AppKit
import WebKit

let args = CommandLine.arguments
guard args.count >= 4 else {
    FileHandle.standardError.write("usage: wk-probe <base> <outDir> <command> ...\n".data(using: .utf8)!)
    exit(2)
}
// Keep the trailing slash: without it "http://host" + "#/jobs" parses to a URL with an
// empty path, which never matches the page the web view is actually on.
let root = args[1].hasSuffix("/") ? args[1] : args[1] + "/"
let base = root
let outDir = args[2]
var commands = Array(args[3...])
try? FileManager.default.createDirectory(atPath: outDir, withIntermediateDirectories: true)

final class Hook: NSObject, WKScriptMessageHandler, WKNavigationDelegate, WKUIDelegate {
    var loadFinished = false
    var loadFailed = false

    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        if let body = message.body as? String { print("  console> " + body) }
    }
    // The .app implements these panels natively; here they are logged and accepted
    // so a flow that stops for confirmation can still be walked end to end.
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        print("  ALERT: " + message)
        completionHandler()
    }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        print("  CONFIRM: " + message)
        completionHandler(true)
    }
    func webView(_ webView: WKWebView, didFinish nav: WKNavigation!) { loadFinished = true }
    func webView(_ webView: WKWebView, didFail nav: WKNavigation!, withError e: Error) { loadFailed = true; loadFinished = true }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation nav: WKNavigation!, withError e: Error) {
        loadFailed = true; loadFinished = true
        print("  NAV FAILED: " + e.localizedDescription)
    }
}

NSApplication.shared.setActivationPolicy(.prohibited)
let config = WKWebViewConfiguration()
let hook = Hook()
let collector = """
window.__probeErrors = [];
window.onerror = function (m, src, line) { window.__probeErrors.push(m + ' @line ' + line); return true; };
window.addEventListener('unhandledrejection', function (e) {
  window.__probeErrors.push('promise: ' + ((e.reason && e.reason.message) || e.reason));
});
(function () {
  var push = function (kind) {
    var text = Array.prototype.slice.call(arguments, 1).join(' ');
    window.__probeErrors.push(kind + ': ' + text);
    if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.probe) {
      window.webkit.messageHandlers.probe.postMessage(kind + ': ' + text);
    }
  };
  console.error = push.bind(null, 'console.error');
  console.warn = push.bind(null, 'console.warn');
})();
"""
let userScript = WKUserScript(source: collector, injectionTime: .atDocumentStart, forMainFrameOnly: true)
config.userContentController.addUserScript(userScript)
config.userContentController.add(hook, name: "probe")
for name in (ProcessInfo.processInfo.environment["WKPROBE_HANDLERS"] ?? "").split(separator: ",") {
    config.userContentController.add(hook, name: String(name).trimmingCharacters(in: .whitespaces))
}
if let script = ProcessInfo.processInfo.environment["WKPROBE_SCRIPT"],
   let source = try? String(contentsOfFile: script, encoding: .utf8) {
    config.userContentController.addUserScript(WKUserScript(source: source, injectionTime: .atDocumentStart,
                                                            forMainFrameOnly: true))
}

let frame = NSRect(x: 0, y: 0, width: 1440, height: 950)
let web = WKWebView(frame: frame, configuration: config)
web.navigationDelegate = hook
web.uiDelegate = hook
let window = NSWindow(contentRect: frame, styleMask: [.titled], backing: .buffered, defer: false)
window.contentView = web
window.setFrameOrigin(NSPoint(x: -4000, y: 0))
window.orderFrontRegardless()

var index = 0
var failures: [String] = []

func spin(_ block: @escaping () -> Void) {
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { block() }
}

func js(_ expression: String, _ done: @escaping (String) -> Void) {
    web.evaluateJavaScript(expression) { value, error in
        if let error = error { done("EVAL ERROR: \(error.localizedDescription)") }
        else if let value = value { done(String(describing: value)) }
        else { done("") }
    }
}

let stateExpr = """
(function () {
  var v = document.getElementById('view');
  var out = [];
  out.push('view:' + (v ? (v.childElementCount + ' blocks, ' + (v.innerText || '').replace(/\\s+/g, ' ').slice(0, 90)) : 'missing'));
  var m = document.getElementById('modal'), d = document.getElementById('drawer');
  out.push('modal:' + (m && !m.hidden ? (m.childElementCount + ' open') : 'closed'));
  out.push('drawer:' + (d && !d.hidden ? 'open' : 'closed'));
  out.push('errors:' + ((window.__probeErrors || []).join(' | ') || 'none'));
  return out.join('  ~  ');
})()
"""

func runNext() {
    if index >= commands.count {
        print("\nSUMMARY: " + (failures.isEmpty ? "all commands ran" : failures.joined(separator: "; ")))
        exit(failures.isEmpty ? 0 : 1)
    }
    let raw = commands[index]
    index += 1
    let parts = raw.components(separatedBy: "|")
    let cmd = parts[0]
    let operand = parts.count > 1 ? parts[1...].joined(separator: "|") : ""

    switch cmd {
    case "open":
        hook.loadFinished = false
        hook.loadFailed = false
        let joined = operand.hasPrefix("/") ? String(operand.dropFirst()) : operand
        let url = operand.hasPrefix("http") ? operand : base + joined
        let target = URL(string: url)!
        print("OPEN " + url)
        let same = web.url != nil
            && web.url!.scheme == target.scheme && web.url!.host == target.host
            && web.url!.path == target.path && web.url!.query == target.query
        if same {
            // Hash-only move: the router renders, WebKit never fires didFinish.
            web.evaluateJavaScript("location.hash = '\(target.fragment ?? "")'") { _, _ in
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { runNext() }
            }
            return
        }
        web.load(URLRequest(url: target))
        var waited = 0.0
        func checkLoaded() {
            if hook.loadFailed {
                failures.append("load failed: " + url)
                runNext()
                return
            }
            if hook.loadFinished {
                // The SPA renders after load, give it a beat before reporting.
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { runNext() }
                return
            }
            // A load that lands on the same document (fragment-only) never reports didFinish,
            // so the address bar agreeing with the target is the real proof of arrival.
            if web.url == target {
                hook.loadFinished = true
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { runNext() }
                return
            }
            waited += 0.1
            if waited > 10 { failures.append("never loaded: " + url); runNext(); return }
            spin { checkLoaded() }
        }
        checkLoaded()
    case "size":
        let dims = operand.components(separatedBy: "x")
        guard dims.count == 2, let wide = Int(dims[0]), let tall = Int(dims[1]) else {
            print("  bad size: " + operand)
            failures.append("size " + operand)
            runNext()
            return
        }
        // A window that is merely moved is not a different screen: the web view has to be the whole
        // content, or a phone-shaped layout is judged on a desktop-sized viewport.
        let content = NSRect(x: 0, y: 0, width: wide, height: tall)
        window.setContentSize(content.size)
        web.frame = content
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
            js("window.innerWidth + 'x' + window.innerHeight") { value in
                print("  viewport now " + value)
                runNext()
            }
        }
    case "poll":
        var waited = 0.0
        func tick() {
            js("(" + operand + ")") { value in
                if value == "true" || value == "1" { runNext(); return }
                waited += 0.2
                if waited > 12 {
                    print("  TIMEOUT waiting for: " + operand)
                    failures.append("timeout: " + operand)
                    runNext()
                    return
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { tick() }
            }
        }
        tick()
    case "eval":
        js(operand + ";\n'ok'") { value in
            if value.hasPrefix("EVAL ERROR") { print("  " + value); failures.append(value) }
            runNext()
        }
    case "say":
        js(operand) { value in print("  " + value) ; runNext() }
    case "state":
        js(stateExpr) { value in print("  " + value); runNext() }
    case "shot":
        let cfg = WKSnapshotConfiguration()
        cfg.rect = CGRect(origin: .zero, size: web.frame.size)
        let path = outDir + "/" + operand + ".png"
        web.takeSnapshot(with: cfg) { image, error in
            if let error = error {
                print("  SNAPSHOT ERROR: " + error.localizedDescription)
                failures.append("snapshot " + operand)
            } else if let image = image,
                      let tiff = image.tiffRepresentation,
                      let rep = NSBitmapImageRep(data: tiff),
                      let png = rep.representation(using: .png, properties: [:]) {
                try? png.write(to: URL(fileURLWithPath: path))
                print("  shot -> " + path)
            } else {
                failures.append("no png for " + operand)
            }
            runNext()
        }
    default:
        print("  unknown command: " + raw)
        runNext()
    }
}

spin { runNext() }
RunLoop.main.run()
