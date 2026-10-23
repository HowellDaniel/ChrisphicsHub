// Chrisphics Hub — the shop book in its own window, on its own computer.
//
// This is the portable install's own application shell, not the shop's. The shop's Mac app attaches
// to the always-on Wi-Fi server and the one shared book; this one starts the engine carried inside
// this .app (Contents/Resources/runtime + app), points its window at it over loopback, and reads and
// writes only this computer's own book.
//
// Objective-C rather than Swift for one reason: the command-line Swift runtime on a Mac without Xcode
// ships arm64 slices only, so a Swift shell cannot be built here for an Intel Mac. clang links
// AppKit and WebKit for both chips from the same file, and nothing needs to be installed on the
// machine that receives the app — not a runtime, not a package, not Python.
//
//     (no arguments)   open the shop in a window, starting the engine first if it is not already up
//     --engine         serve this computer's book and stay in the terminal — no window, no browser
//     --port N         the loopback port to use (default 8712); CHRISPHICS_PORT works too
//     --window-probe D open the shop, write what its own window shows into folder D, and close

#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#import <WebKit/WebKit.h>
#import <signal.h>

static NSString *const kName = @"Chrisphics Hub";
static NSInteger kPort = 8712;
static NSString *kBase = nil;
static NSTask *serving = nil;
static NSString *kProbeTo = nil;

// Where a portable install keeps its records: this user's own folder, and a book we name. The name
// matters — server.py only goes looking for an older book when it is left to pick its own path, and
// a copy that found another shop's records would print them on this computer's screens. HOME is read
// first, so a copy started under a different home — as the build check does — writes there.
static NSString *supportFolder(void) {
    NSString *home = [NSProcessInfo processInfo].environment[@"HOME"];
    if (home.length == 0) home = NSHomeDirectory();
    return [home stringByAppendingPathComponent:@"Library/Application Support/CRISPprint Hub"];
}

static NSString *urlPath(NSString *tail) {
    return [kBase stringByAppendingString:tail];
}

// Is the shop already answering on this port? Bounded by the request's own timeout: an unbounded
// connect is how a check hangs instead of failing.
static BOOL engineUp(NSTimeInterval wait) {
    NSMutableURLRequest *request = [NSMutableURLRequest requestWithURL:[NSURL URLWithString:urlPath(@"healthz")]];
    request.timeoutInterval = wait;
    NSURLSessionConfiguration *config = [NSURLSessionConfiguration ephemeralSessionConfiguration];
    config.timeoutIntervalForRequest = wait;
    NSURLSession *session = [NSURLSession sessionWithConfiguration:config];
    __block NSString *answer = nil;
    dispatch_semaphore_t settled = dispatch_semaphore_create(0);
    [[session dataTaskWithRequest:request
               completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
                   answer = data ? [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding] : nil;
                   dispatch_semaphore_signal(settled);
               }] resume];
    if (dispatch_semaphore_wait(settled, dispatch_time(DISPATCH_TIME_NOW,
                                                       (int64_t)((wait + 0.5) * NSEC_PER_SEC)))) return NO;
    return answer && [answer rangeOfString:@"ok"].location != NSNotFound;
}

static NSTask *startEngine(void) {
    NSString *resources = [NSBundle mainBundle].resourcePath;
    NSString *python = [resources stringByAppendingPathComponent:@"runtime/bin/python3"];
    NSString *server = [resources stringByAppendingPathComponent:@"app/server.py"];
    NSString *support = supportFolder();
    NSFileManager *disk = [NSFileManager defaultManager];
    [disk createDirectoryAtPath:support withIntermediateDirectories:YES attributes:nil error:nil];
    if (![disk isExecutableFileAtPath:python] || ![disk fileExistsAtPath:server]) return nil;

    NSTask *child = [[NSTask alloc] init];
    child.executableURL = [NSURL fileURLWithPath:python];
    child.arguments = @[ server, @"--port", [NSString stringWithFormat:@"%ld", (long)kPort],
                         @"--no-browser" ];
    NSMutableDictionary *environment = [[NSProcessInfo processInfo].environment mutableCopy];
    environment[@"CHRISPHICS_SUPPORT"] = support;
    environment[@"CHRISPHICS_DB"] = [support stringByAppendingPathComponent:@"shop.db"];
    child.environment = environment;

    // What the engine says is the only record a person has when their copy will not open, so it goes
    // in the same folder as their book rather than into a window that already closed.
    NSString *log = [support stringByAppendingPathComponent:@"engine.log"];
    NSFileHandle *handle = [NSFileHandle fileHandleForWritingAtPath:log];
    if (!handle) {
        [disk createFileAtPath:log contents:nil attributes:nil];
        handle = [NSFileHandle fileHandleForWritingAtPath:log];
    }
    child.standardOutput = handle;
    child.standardError = handle;
    NSError *failure = nil;
    if (![child launchAndReturnError:&failure] || failure) return nil;
    return child;
}

// Whatever ends this process ends the shop it started with it. A person closing the window, a
// `kill`, and the build check calling terminate() all arrive here; leaving the engine behind is how
// the next launch quietly joins a process nobody started on purpose — and how a port stays held.
static void closeShop(int number) {
    [serving terminate];
    exit(0);
}

static void listenForClosing(void) {
    signal(SIGTERM, closeShop);
    signal(SIGINT, closeShop);
    signal(SIGHUP, closeShop);
}

// ------------------------------------------------------------ the engine-only door
// A person who wants the shop in a browser tab, and the build check that has to prove a bundle runs
// without taking over a screen, both come in here.
static int runEngineOnly(void) {
    if (engineUp(2.0)) {
        printf("Chrisphics Hub is already open at %s\n", kBase.UTF8String);
        fflush(stdout);
        return 0;
    }
    NSString *support = supportFolder();
    // Before the launch, not after the shop is known to be up: the engine has the port open about a
    // second later, and a `kill` landing in that first second would otherwise take this process down
    // by its default action and leave the shop listening behind it with nobody owning it.
    listenForClosing();
    serving = startEngine();
    if (!serving) {
        printf("This copy is missing its own Python or its own shop files.\n");
        fflush(stdout);
        return 1;
    }
    for (int attempt = 0; attempt < 80 && !engineUp(1.0); attempt++) usleep(250000);
    printf("Chrisphics Hub is open at %s\n", kBase.UTF8String);
    printf("Records: %s/shop.db   What the shop said: %s/engine.log\n",
           support.UTF8String, support.UTF8String);
    fflush(stdout);
    [serving waitUntilExit];
    return serving.terminationStatus == 0 ? 0 : 1;
}

// ------------------------------------------------------------ the application
@interface ShopWindow : NSObject <NSApplicationDelegate, WKNavigationDelegate>
@end

@implementation ShopWindow {
    NSWindow *_window;
    WKWebView *_web;
    BOOL _shown;
    NSInteger _tries;
}

- (void)applicationDidFinishLaunching:(NSNotification *)note {
    [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
    [NSApp activateIgnoringOtherApps:YES];
    [self setMenu];

    _window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 1280, 820)
                                          styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                                                     NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
                                            backing:NSBackingStoreBuffered
                                              defer:NO];
    _window.title = kName;
    _window.releasedWhenClosed = NO;
    _window.minSize = NSMakeSize(420, 480);
    [_window center];
    [_window setFrameAutosaveName:@"Chrisphics Hub window"];

    _web = [[WKWebView alloc] initWithFrame:_window.contentView.bounds];
    _web.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    _web.navigationDelegate = self;
    [_window.contentView addSubview:_web];
    [_window makeKeyAndOrderFront:nil];

    if (!engineUp(1.0)) serving = startEngine();
    [self showWhenReady];
}

// The engine takes a second or two to open its book; the window asks again until it answers rather
// than painting a connection error at someone who did nothing wrong.
- (void)showWhenReady {
    if (engineUp(1.0) || ++_tries > 120) {
        BOOL timedOut = _tries > 120;
        dispatch_async(dispatch_get_main_queue(), ^{ [self openShop:timedOut]; });
        return;
    }
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.25 * NSEC_PER_SEC)),
                   dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{ [self showWhenReady]; });
}

- (void)openShop:(BOOL)timedOut {
    if (_shown) return;
    _shown = YES;
    NSString *support = supportFolder();
    if (timedOut) {
        NSString *why = serving ? @"the shop did not answer in time"
                                : @"this copy is missing its own Python or its own shop files";
        NSString *html = [NSString stringWithFormat:
            @"<html><body style=\"font:16px -apple-system;padding:40px;color:#404040\">"
            @"<h2>%@ could not open</h2><p>%@.</p>"
            @"<p style=\"color:#808080\">What the shop said is in <code>%@/engine.log</code>, and "
            @"your records are in <code>%@/shop.db</code>.</p></body></html>",
            kName, why, support, support];
        [_web loadHTMLString:html baseURL:nil];
        return;
    }
    [_web loadRequest:[NSURLRequest requestWithURL:[NSURL URLWithString:kBase]]];
}

// ------------------------------------------------------------ the window's own report
// Whether the shop appears in the window is the first question about a Mac app, and neither half of
// it can be answered from outside: a screen grab of the whole display comes back black when the
// display has gone to sleep, and a page that arrived over loopback is not yet a page that painted.
// So the app is allowed to describe its own window. --window-probe opens the shop, waits for the web
// view to finish, then writes two things into the folder it is given: window.txt (what the page says
// it is, straight out of its DOM, plus how many distinct colours and how wide a spread of light the
// picture holds) and window.png (a picture of the window and nothing else). An unpainted rectangle
// has one colour and no spread, which is exactly the failure to catch.

- (void)webView:(WKWebView *)view didFinishNavigation:(WKNavigation *)navigation {
    if (!kProbeTo) return;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.75 * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{ [self report]; });
}

- (void)webView:(WKWebView *)view didFailNavigation:(WKNavigation *)navigation
      withError:(NSError *)error {
    if (!kProbeTo) return;
    [self finishWith:[NSString stringWithFormat:@"the window could not load the shop: %@\n",
                                                error.localizedDescription]];
}

- (void)report {
    NSMutableDictionary *found = [NSMutableDictionary dictionary];
    dispatch_group_t both = dispatch_group_create();

    dispatch_group_enter(both);
    [_web evaluateJavaScript:@"JSON.stringify({title: document.title,"
                             @" screens: document.querySelectorAll('.navlink').length,"
                             @" status: (document.getElementById('networkStatus') || {}).innerText || '',"
                             @" wide: Math.round(innerWidth), high: Math.round(innerHeight)})"
           completionHandler:^(id result, NSError *error) {
               found[@"page"] = error ? [NSString stringWithFormat:@"nothing: %@",
                                        error.localizedDescription] : [NSString stringWithFormat:@"%@", result];
               dispatch_group_leave(both);
           }];

    dispatch_group_enter(both);
    [_web takeSnapshotWithConfiguration:[[WKSnapshotConfiguration alloc] init]
                      completionHandler:^(NSImage *image, NSError *error) {
                          if (!image) {
                              found[@"picture"] = [NSString stringWithFormat:@"no picture: %@",
                                                   error.localizedDescription];
                              dispatch_group_leave(both);
                              return;
                          }
                          found[@"picture"] = [self describe:image];
                          dispatch_group_leave(both);
                      }];

    dispatch_group_notify(both, dispatch_get_main_queue(), ^{
        NSMutableString *text = [NSMutableString string];
        for (NSString *key in @[ @"page", @"picture" ])
            [text appendFormat:@"%@: %@\n", key, found[key] ?: @"missing"];
        [self finishWith:text];
    });
}

// Draws the snapshot into bytes this code controls, counts the colours in it, saves the picture and
// reports the spread. The image WebKit hands back does not carry a plain bitmap representation, so
// its own pixels cannot be read; redrawing it at the window's backing scale gives a buffer whose
// layout is known instead. Sampling every third pixel keeps a full-size window to a few hundred
// thousand reads; five bits a channel means two neighbouring shades of the same card count once, and
// a black rectangle still counts once.
- (NSString *)describe:(NSImage *)image {
    CGFloat scale = _window ? _window.backingScaleFactor : 2.0;
    NSInteger wide = (NSInteger)llround(image.size.width * scale);
    NSInteger high = (NSInteger)llround(image.size.height * scale);
    if (wide < 16 || high < 16) return [NSString stringWithFormat:@"a picture %ld x %ld wide", (long)wide, (long)high];

    NSBitmapImageRep *rep = [[NSBitmapImageRep alloc]
        initWithBitmapDataPlanes:NULL pixelsWide:wide pixelsHigh:high
                    bitsPerSample:8 samplesPerPixel:4 hasAlpha:YES isPlanar:NO
                   colorSpaceName:NSDeviceRGBColorSpace bytesPerRow:0 bitsPerPixel:0];
    if (!rep) return @"a picture that could not be read";
    NSGraphicsContext *context = [NSGraphicsContext graphicsContextWithBitmapImageRep:rep];
    [NSGraphicsContext saveGraphicsState];
    [NSGraphicsContext setCurrentContext:context];
    [image drawInRect:NSMakeRect(0, 0, wide, high)];
    [NSGraphicsContext restoreGraphicsState];

    unsigned char *bytes = [rep bitmapData];
    NSInteger stride = [rep bytesPerRow];
    NSInteger depth = [rep samplesPerPixel];
    NSMutableSet *colours = [NSMutableSet set];
    NSInteger least = 255, most = 0;
    for (NSInteger y = 0; y < high; y += 3) {
        for (NSInteger x = 0; x < wide; x += 3) {
            unsigned char *p = bytes + y * stride + x * depth;
            NSInteger red = p[0], green = p[1], blue = p[2];
            [colours addObject:@(((red >> 3) << 10) | ((green >> 3) << 5) | (blue >> 3))];
            NSInteger light = (red * 30 + green * 59 + blue * 11) / 100;
            if (light < least) least = light;
            if (light > most) most = light;
        }
    }
    NSData *png = [rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}];
    BOOL saved = [png writeToFile:[kProbeTo stringByAppendingPathComponent:@"window.png"]
                       atomically:YES];
    return [NSString stringWithFormat:@"%ld x %ld pixels, %lu distinct colours, light from %ld to %ld%@",
                                       (long)wide, (long)high, (unsigned long)colours.count,
                                       (long)least, (long)most, saved ? @"" : @" (picture not saved)"];
}

- (void)finishWith:(NSString *)text {
    NSString *folder = kProbeTo;
    kProbeTo = nil;
    if (folder) {
        [[NSFileManager defaultManager] createDirectoryAtPath:folder withIntermediateDirectories:YES
                                                   attributes:nil error:nil];
        [text writeToFile:[folder stringByAppendingPathComponent:@"window.txt"]
               atomically:YES encoding:NSUTF8StringEncoding error:nil];
    }
    printf("%s", [text UTF8String]);
    fflush(stdout);
    [serving terminate];
    exit(0);
}

- (void)setMenu {
    NSMenu *main = [[NSMenu alloc] init];
    NSMenuItem *appItem = [[NSMenuItem alloc] init];
    [main addItem:appItem];
    NSMenu *appMenu = [[NSMenu alloc] init];
    [appMenu addItemWithTitle:[@"About " stringByAppendingString:kName]
                       action:@selector(orderFrontStandardAboutPanel:) keyEquivalent:@""];
    [appMenu addItem:[NSMenuItem separatorItem]];
    [appMenu addItemWithTitle:[@"Hide " stringByAppendingString:kName]
                       action:@selector(hide:) keyEquivalent:@"h"];
    NSMenuItem *others = [appMenu addItemWithTitle:@"Hide Others"
                                            action:@selector(hideOtherApplications:) keyEquivalent:@"h"];
    others.keyEquivalentModifierMask = NSEventModifierFlagOption | NSEventModifierFlagCommand;
    [appMenu addItemWithTitle:@"Show All" action:@selector(unhideAllApplications:) keyEquivalent:@""];
    [appMenu addItem:[NSMenuItem separatorItem]];
    [appMenu addItemWithTitle:[@"Quit " stringByAppendingString:kName]
                       action:@selector(terminate:) keyEquivalent:@"q"];
    appItem.submenu = appMenu;

    // Without an Edit menu the standard shortcuts do not reach the web view, and a cashier who cannot
    // paste a client's phone number into the counter reasonably calls that broken.
    NSMenuItem *editItem = [[NSMenuItem alloc] init];
    [main addItem:editItem];
    NSMenu *edit = [[NSMenu alloc] initWithTitle:@"Edit"];
    [edit addItemWithTitle:@"Cut" action:@selector(cut:) keyEquivalent:@"x"];
    [edit addItemWithTitle:@"Copy" action:@selector(copy:) keyEquivalent:@"c"];
    [edit addItemWithTitle:@"Paste" action:@selector(paste:) keyEquivalent:@"v"];
    [edit addItemWithTitle:@"Select All" action:@selector(selectAll:) keyEquivalent:@"a"];
    editItem.submenu = edit;

    NSMenuItem *windowItem = [[NSMenuItem alloc] init];
    [main addItem:windowItem];
    NSMenu *windows = [[NSMenu alloc] initWithTitle:@"Window"];
    [windows addItemWithTitle:@"Minimise" action:@selector(miniaturize:) keyEquivalent:@"m"];
    [windows addItemWithTitle:@"Zoom" action:@selector(zoom:) keyEquivalent:@""];
    windowItem.submenu = windows;

    NSApp.mainMenu = main;
    NSApp.windowsMenu = windows;
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)app { return YES; }

- (void)applicationWillTerminate:(NSNotification *)note {
    // Closing the app closes the shop it started, so a second copy later opens its own rather than
    // quietly joining a process nobody left on purpose.
    [serving terminate];
}

- (BOOL)applicationShouldHandleReopen:(NSApplication *)app hasVisibleWindows:(BOOL)visible {
    if (!visible) [_window makeKeyAndOrderFront:nil];
    return YES;
}

@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        for (int i = 1; i < argc; i++) {
            if (!strcmp(argv[i], "--port") && i + 1 < argc) kPort = atol(argv[i + 1]);
            if (!strcmp(argv[i], "--window-probe") && i + 1 < argc)
                kProbeTo = [NSString stringWithUTF8String:argv[i + 1]];
        }
        // Made here rather than at the first write, because the picture of the window is written
        // before the text that describes it, and a write into a folder that is not there fails quietly.
        if (kProbeTo) [[NSFileManager defaultManager] createDirectoryAtPath:kProbeTo
                                               withIntermediateDirectories:YES attributes:nil error:nil];
        NSString *fromEnvironment = [NSProcessInfo processInfo].environment[@"CHRISPHICS_PORT"];
        if (kPort == 8712 && fromEnvironment.length) kPort = fromEnvironment.integerValue;
        kBase = [NSString stringWithFormat:@"http://127.0.0.1:%ld/", (long)kPort];

        for (int i = 1; i < argc; i++) {
            if (!strcmp(argv[i], "--engine") || !strcmp(argv[i], "--no-browser"))
                return runEngineOnly();
        }
        NSApplication *app = [NSApplication sharedApplication];
        static ShopWindow *delegate;
        delegate = [[ShopWindow alloc] init];
        app.delegate = delegate;
        listenForClosing();
        [app run];
        return 0;
    }
}
