// Chrisphics Hub — the shop book in its own window, on its own computer.
//
// This is the portable install's own application shell, not the shop's. On any other computer it
// starts the engine carried inside this .app (Contents/Resources/runtime + app), points its window
// at it over loopback, and reads and writes only this computer's own book — a book that, whenever
// this machine does not have one yet or still has the empty one it made for itself, is filled from
// the copy of the shop's records the build put inside the app.
//
// On the shop's own Mac that copy is yesterday's book beside today's, so before starting anything
// the shell asks the shop's always-on server — the same loopback door the shop's Mac app uses — and,
// when it answers under the shop's own certificate, shows that live book instead and starts no
// engine of its own. Every machine that is not the shop gets the standalone behaviour above.
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
//     --settings-probe D  open Settings, write what each pane says into folder D, and close

#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#import <WebKit/WebKit.h>
#import <Security/Security.h>
#import <fcntl.h>
#import <signal.h>

static NSString *const kName = @"Chrisphics Hub";
static NSInteger kPort = 8712;
static NSString *kBase = nil;
static NSTask *serving = nil;
static NSString *kProbeTo = nil;
static NSString *kSettingsProbeTo = nil;
// The settings window and the shop window share these three: the page they size, the sign-in
// preference that outlives a launch, and the caffeinate that holds this Mac awake while it runs.
static WKWebView *kShopWeb = nil;
static NSTask *keepingAwake = nil;
static NSString *const kZoomKey = @"ChrisphicsPageSize";
static NSString *const kStayAwakeKey = @"ChrisphicsStayAwake";
// The page's background, picked in Settings ▸ Appearance and kept on this computer. "plain" is the
// shop's own paper; anything else is told to the page before it paints and held across launches.
static NSString *const kBackgroundKey = @"crispprint-background";
// True when the window is showing the shop's live book on this Mac rather than this computer's copy.
static BOOL kOnShopBook = NO;
static void holdAwakeNow(BOOL on);

static NSArray<NSArray<NSString *> *> *backgroundChoices(void) {
    return @[ @[ @"plain", @"Plain — the shop's own paper" ],
              @[ @"sky", @"Sky" ],
              @[ @"leaf", @"Leaf" ],
              @[ @"sand", @"Sand" ],
              @[ @"rose", @"Rose" ],
              @[ @"waves", @"Waves wallpaper" ],
              @[ @"dots", @"Dots wallpaper" ],
              @[ @"linen", @"Linen wallpaper" ],
              @[ @"violet", @"Violet lines" ],
              @[ @"beams", @"Teal beams" ],
              @[ @"circuit", @"Circuit" ],
              @[ @"constellation", @"Constellation" ],
              @[ @"dusk", @"Dusk" ],
              @[ @"bloom", @"Bloom" ] ];
}

static NSString *chosenBackground(void) {
    NSString *saved = [[NSUserDefaults standardUserDefaults] stringForKey:kBackgroundKey];
    return saved.length ? saved : @"plain";
}

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

// The menu's actions are written beside the menu, above the helpers they lean on.
static NSString *bookFile(void);
static NSString *copiesFolder(void);
static void callEngine(NSString *method, NSString *path, NSDictionary *body,
                       void (^done)(NSDictionary *json, NSString *why));

// A copy built by the shop's own build tool arrives with the shop's records inside it, at
// Contents/Resources/data/shop.db, so the first screen a person sees holds the shop's figures and the
// shop's settings instead of an empty ledger. The app does not copy that file itself: it hands the path
// to the engine with --shop-copy and the engine decides, once, in one place shared with the .command,
// the Windows .bat and the installer. It takes the copy whenever this machine's own book is missing or
// still the empty one the engine made for itself — so a Mac that was shown the demo build first does
// not stay blank after installing the shop's copy — and never touches a book that holds a client, a
// job, a payment or any other shop record. Settings and sign-in history alone do not protect a book
// with no records: that is the copy an installer should fill. After that this machine writes its own
// book and the shop Mac writes its own.
static NSString *shopCopyInsideBundle(void) {
    return [[NSBundle mainBundle].resourcePath stringByAppendingPathComponent:@"data/shop.db"];
}

// One descriptor, opened for appending, for everything the engine prints. A handle from
// `fileHandleForWritingAtPath:` is given to stdout and stderr separately, and each of those writers
// keeps its own position in the file: the request log starts at the top and lays itself over whatever
// the engine said first. O_APPEND is a property of the open file, not of the writer, so every write
// lands after the last one whoever made it.
static NSFileHandle *openForAppend(NSString *path) {
    int descriptor = open([path fileSystemRepresentation], O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (descriptor < 0) return nil;
    return [[NSFileHandle alloc] initWithFileDescriptor:descriptor closeOnDealloc:YES];
}

// ------------------------------------------------------------ the shop already serving here
//
// A hand-out copy owns its own book, and on the shop's own Mac that is the wrong book: the counter
// trades in the live one, and a copy that only ever holds the day it was built quietly answers
// yesterday's questions. So before starting anything, the shell asks the shop's always-on server on
// the loopback door the shop's own Mac app already uses, and shows that book when it answers. A
// second engine over the live book is the one thing that spoils it, so this never starts one beside
// it. Every other computer this app is handed to hears nothing there and behaves as it always has.
//
// Nothing but the shop's own certificate is trusted. The authority lives in two places: this Mac's
// copy under Application Support, and the copy a hand-out bundle carries beside its engine. The
// second is what lets an iMac on the shop's Wi-Fi trust the shop's one book without anyone visiting
// Keychain Access. It is the same authority either way, and the address is not what makes a server
// the shop — a certificate this authority did not issue is refused wherever it is offered.

static BOOL onThisMac(NSString *host) {
    return host.length > 0 &&
           ([host isEqualToString:@"localhost"] || [host isEqualToString:@"::1"] ||
            [host hasPrefix:@"127."]);
}

static NSString *bundleResource(NSString *name) {
    NSString *path = [[NSBundle mainBundle].resourcePath stringByAppendingPathComponent:name];
    return [[NSFileManager defaultManager] fileExistsAtPath:path] ? path : nil;
}

static NSString *homePath(NSString *tail) {
    NSString *home = [NSProcessInfo processInfo].environment[@"HOME"];
    if (home.length == 0) home = NSHomeDirectory();
    return [home stringByAppendingPathComponent:tail];
}

static SecCertificateRef certificateFromPEM(NSString *path) {
    NSString *text = path ? [NSString stringWithContentsOfFile:path encoding:NSUTF8StringEncoding
                                                         error:nil] : nil;
    if (!text) return NULL;
    NSMutableString *base64 = [NSMutableString string];
    for (NSString *line in [text componentsSeparatedByCharactersInSet:
                                    [NSCharacterSet newlineCharacterSet]]) {
        NSString *trimmed = [line stringByTrimmingCharactersInSet:
                                     [NSCharacterSet whitespaceCharacterSet]];
        if (trimmed.length == 0 || [trimmed hasPrefix:@"-----"]) continue;
        [base64 appendString:trimmed];
    }
    NSData *der = [[NSData alloc] initWithBase64EncodedString:base64 options:0];
    return der ? SecCertificateCreateWithData(NULL, (__bridge CFDataRef)der) : NULL;
}

static SecCertificateRef shopAuthority(void) {
    SecCertificateRef anchor = certificateFromPEM(
        homePath(@"Library/Application Support/mkcert/rootCA.pem"));
    return anchor ?: certificateFromPEM(bundleResource(@"shop-ca.pem"));
}

// The certificate chain a server offered. The modern reader arrived in macOS 12, and this app is
// handed to Macs older than that, so the older one carries them.
static NSArray *shopChain(SecTrustRef trust) {
    if (@available(macOS 12.0, *)) {
        NSArray *modern = (__bridge_transfer NSArray *)SecTrustCopyCertificateChain(trust);
        return modern ?: @[];
    }
    NSMutableArray *chain = [NSMutableArray array];
    CFIndex count = SecTrustGetCertificateCount(trust);
    for (CFIndex i = 0; i < count; i++) {
        SecCertificateRef certificate = SecTrustGetCertificateAtIndex(trust, i);
        if (certificate) [chain addObject:(__bridge id)certificate];
    }
    return chain;
}

static BOOL shopTrusted(SecTrustRef trust) {
    if (@available(macOS 10.14, *)) return SecTrustEvaluateWithError(trust, NULL);
    SecTrustResultType result = kSecTrustResultInvalid;
    return SecTrustEvaluate(trust, &result) == errSecSuccess &&
           (result == kSecTrustResultProceed || result == kSecTrustResultUnspecified);
}

static BOOL shopChainTrusted(SecTrustRef offered, NSString *host) {
    if (!offered || host.length == 0) return NO;
    SecCertificateRef anchor = shopAuthority();
    if (!anchor) return NO;
    NSArray *chain = shopChain(offered);
    SecPolicyRef policy = SecPolicyCreateSSL(true, (__bridge CFStringRef)host);
    SecTrustRef checked = NULL;
    BOOL fine = NO;
    if (chain.count && policy &&
        SecTrustCreateWithCertificates((__bridge CFArrayRef)chain, policy, &checked) == errSecSuccess) {
        SecTrustSetAnchorCertificates(checked, (__bridge CFArrayRef)@[ (__bridge id)anchor ]);
        SecTrustSetAnchorCertificatesOnly(checked, true);
        fine = shopTrusted(checked);
    }
    if (checked) CFRelease(checked);
    if (policy) CFRelease(policy);
    CFRelease(anchor);
    return fine;
}

// Anything whose certificate is not the shop's own is turned away rather than handed back to the
// system's judgement. The shop Mac has its authority in the login keychain as well, so falling back
// to the default would let a build check — which runs under a throwaway HOME with no authority of
// its own — quietly join the live book and never start the engine it was checking.
@interface ShopDoor : NSObject <NSURLSessionDelegate>
@end

@implementation ShopDoor
- (void)URLSession:(NSURLSession *)session
    didReceiveChallenge:(NSURLAuthenticationChallenge *)challenge
      completionHandler:(void (^)(NSURLSessionAuthChallengeDisposition, NSURLCredential *))completionHandler {
    NSURLProtectionSpace *space = challenge.protectionSpace;
    if ([space.authenticationMethod isEqualToString:NSURLAuthenticationMethodServerTrust]) {
        if (shopChainTrusted(space.serverTrust, space.host)) {
            return completionHandler(NSURLSessionAuthChallengeUseCredential,
                                     [NSURLCredential credentialForTrust:space.serverTrust]);
        }
        return completionHandler(NSURLSessionAuthChallengeCancelAuthenticationChallenge, nil);
    }
    completionHandler(NSURLSessionAuthChallengePerformDefaultHandling, nil);
}
@end

// One session for every call into the shop, so the certificate decision is made in one place.
static NSURLSession *shopSession(void) {
    static NSURLSession *session = nil;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        NSURLSessionConfiguration *config = [NSURLSessionConfiguration ephemeralSessionConfiguration];
        config.timeoutIntervalForRequest = 10;
        ShopDoor *door = [[ShopDoor alloc] init];
        session = [NSURLSession sessionWithConfiguration:config delegate:door delegateQueue:nil];
    });
    return session;
}

// Is the shop already answering at this address? Bounded by the request's own timeout: an unbounded
// connect is how a check hangs instead of failing.
static BOOL shopAnswers(NSURL *base, NSTimeInterval wait) {
    if (!base) return NO;
    NSMutableURLRequest *request = [NSMutableURLRequest requestWithURL:
                                    [NSURL URLWithString:@"healthz" relativeToURL:base]];
    request.timeoutInterval = wait;
    __block BOOL answered = NO;
    dispatch_semaphore_t settled = dispatch_semaphore_create(0);
    [[shopSession() dataTaskWithRequest:request
                     completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
        NSString *body = data ? [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding] : nil;
        answered = (error == nil && [(NSHTTPURLResponse *)response statusCode] == 200 &&
                    [body rangeOfString:@"ok"].location != NSNotFound);
        dispatch_semaphore_signal(settled);
    }] resume];
    if (dispatch_semaphore_wait(settled, dispatch_time(DISPATCH_TIME_NOW,
                                                       (int64_t)((wait + 0.5) * NSEC_PER_SEC)))) return NO;
    return answered;
}

static BOOL engineUp(NSTimeInterval wait) {
    return shopAnswers([NSURL URLWithString:kBase], wait);
}

// The address a person chose for the shop on this Mac, kept across launches. Empty means this Mac
// has not joined one and keeps its own book.
static NSString *const kShopAddressKey = @"ChrisphicsShopAddress";
// Somebody who asked for this Mac's own book keeps it on the next launch too; without this, the
// shop found on the Wi-Fi would quietly take the window back.
static NSString *const kOwnBookKey = @"ChrisphicsOwnBook";

static NSString *savedShopAddress(void) {
    NSString *saved = [[NSUserDefaults standardUserDefaults] stringForKey:kShopAddressKey];
    return saved.length ? saved : @"";
}

static void rememberShop(NSString *address) {
    if (address.length) [[NSUserDefaults standardUserDefaults] setObject:address
                                                                forKey:kShopAddressKey];
    else [[NSUserDefaults standardUserDefaults] removeObjectForKey:kShopAddressKey];
}

// The shop's own answers, in the order this copy tries them: the one named in the environment (the
// build checks and the .command use that), the one chosen here, this Mac's loopback door for the
// shop Mac itself, and the addresses the installer carried — written from the shop's own answer
// when the bundle was built, so an iMac on the shop's Wi-Fi finds it without anyone typing an IP.
static NSArray<NSString *> *shopCandidates(void) {
    NSMutableArray<NSString *> *found = [NSMutableArray array];
    NSString *named = [NSProcessInfo processInfo].environment[@"CHRISPHICS_SHOP_URL"];
    if (named.length) [found addObject:named];
    if (savedShopAddress().length) [found addObject:savedShopAddress()];
    NSString *port = [NSProcessInfo processInfo].environment[@"CHRISPHICS_SHOP_PORT"];
    if (port.length == 0) port = @"8834";
    [found addObject:[NSString stringWithFormat:@"https://127.0.0.1:%@/", port]];
    NSString *listed = bundleResource(@"shop-urls.txt");
    if (listed) {
        NSString *text = [NSString stringWithContentsOfFile:listed encoding:NSUTF8StringEncoding
                                                      error:nil];
        for (NSString *line in [text componentsSeparatedByCharactersInSet:
                                        [NSCharacterSet newlineCharacterSet]]) {
            NSString *url = [line stringByTrimmingCharactersInSet:
                                    [NSCharacterSet whitespaceCharacterSet]];
            if (url.length == 0 || [url hasPrefix:@"#"] || ![url hasPrefix:@"https://"]) continue;
            if (![url hasSuffix:@"/"]) url = [url stringByAppendingString:@"/"];
            if (![found containsObject:url]) [found addObject:url];
        }
    }
    return found;
}

static NSURL *shopAt(NSString *address) {
    if (address.length == 0) return nil;
    if (![address hasSuffix:@"/"]) address = [address stringByAppendingString:@"/"];
    NSURL *base = [NSURL URLWithString:address];
    return base.host.length ? base : nil;
}

// The hosts this copy calls the shop's own: its loopback door and every address it tries. A page
// asking one of these must present the shop's certificate, never a stranger's. Everything else the
// page asks for — the forecast service — is the system's judgement, the same as in a browser; the
// shell used to turn every other host away, which silenced the weather on every installed Mac.
static BOOL shopOwnHost(NSString *host) {
    if (host.length == 0) return NO;
    NSString *asked = host.lowercaseString;
    if ([asked isEqualToString:@"localhost"] || [asked isEqualToString:@"127.0.0.1"] ||
        [asked isEqualToString:@"::1"]) return YES;
    for (NSString *address in shopCandidates()) {
        NSString *candidate = shopAt(address).host.lowercaseString;
        if (candidate.length && [candidate isEqualToString:asked]) return YES;
    }
    return NO;
}

// The shop, if it is serving. A copy that has not joined one finds it on the shop Mac's own door or
// at an address that came with the installer; a copy that has joined knows where to look first.
static NSURL *liveShop(void) {
    for (NSString *address in shopCandidates()) {
        NSURL *base = shopAt(address);
        if (shopAnswers(base, 1.5)) return base;
    }
    return nil;
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
    // The engine decides whether this machine has a book of its own to keep, so the rule is written
    // once — here, in the .command, and in the Windows .bat all hand over the same path.
    child.arguments = @[ server, @"--port", [NSString stringWithFormat:@"%ld", (long)kPort],
                         @"--no-browser", @"--shop-copy", shopCopyInsideBundle() ];
    NSMutableDictionary *environment = [[NSProcessInfo processInfo].environment mutableCopy];
    environment[@"CHRISPHICS_SUPPORT"] = support;
    environment[@"CHRISPHICS_DB"] = [support stringByAppendingPathComponent:@"shop.db"];
    child.environment = environment;

    // What the engine says is the only record a person has when their copy will not open, so it goes
    // in the same folder as their book rather than into a window that already closed. Both streams go
    // to one descriptor opened with O_APPEND: a plain writing handle leaves each of them its own
    // position, and the request log then writes over the top of the file — which is how the line that
    // says what happened to the shop's book went missing from a copy that had, in fact, taken it.
    NSString *log = [support stringByAppendingPathComponent:@"engine.log"];
    NSFileHandle *handle = openForAppend(log);
    if (!handle) {
        [disk createFileAtPath:log contents:nil attributes:nil];
        handle = openForAppend(log);
    }
    if (handle) {
        child.standardOutput = handle;
        child.standardError = handle;
    }
    NSError *failure = nil;
    if (![child launchAndReturnError:&failure] || failure) return nil;
    return child;
}

// ------------------------------------------------------------ one book, or this Mac's own
// Which book the window shows is decided once at launch and can be changed in Settings: the shop's
// one book over the shop's Wi-Fi, or a book this Mac keeps to itself. A copy left on the shop's
// address while the shop cannot be reached is not a broken copy — the page's own queue holds what is
// typed until the shop answers again, and nothing is written into a second book that would later
// have to be brought in by hand.

static NSString *localBase(void) {
    return [NSString stringWithFormat:@"http://127.0.0.1:%ld/", (long)kPort];
}

static void openInWindow(NSString *base) {
    kBase = base;
    if (kShopWeb) [kShopWeb loadRequest:[NSURLRequest requestWithURL:[NSURL URLWithString:kBase]]];
}

static void joinShop(NSString *address) {
    if (serving) { [serving terminate]; serving = nil; }
    NSURL *base = shopAt(address);
    if (!base) return;
    rememberShop(base.absoluteString);
    [[NSUserDefaults standardUserDefaults] setBool:NO forKey:kOwnBookKey];
    kOnShopBook = YES;
    openInWindow(base.absoluteString);
}

static void showLocalWhenReady(void) {
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        for (int attempt = 0; attempt < 40 && !engineUp(0.5); attempt++) usleep(250000);
        dispatch_async(dispatch_get_main_queue(), ^{
            if (!kOnShopBook) openInWindow(localBase());
        });
    });
}

static void useOwnBook(void) {
    rememberShop(@"");
    [[NSUserDefaults standardUserDefaults] setBool:YES forKey:kOwnBookKey];
    if (serving) { [serving terminate]; serving = nil; }
    kOnShopBook = NO;
    kBase = localBase();
    if (!engineUp(1.0)) serving = startEngine();
    showLocalWhenReady();
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
@interface ShopSettings : NSObject
+ (void)show;
+ (void)probeInto:(NSString *)folder done:(void (^)(void))done;
@end

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

    // The background chosen in Settings ▸ Appearance is told to the page before it paints, so a
    // relaunch opens already wearing it and never flashes the shop's own paper first.
    WKWebViewConfiguration *config = [[WKWebViewConfiguration alloc] init];
    NSString *background = chosenBackground();
    if (![background isEqualToString:@"plain"]) {
        WKUserContentController *controller = [[WKUserContentController alloc] init];
        [controller addUserScript:[[WKUserScript alloc]
            initWithSource:[NSString stringWithFormat:@"document.documentElement.dataset.bg='%@';",
                                                      background]
            injectionTime:WKUserScriptInjectionTimeAtDocumentStart forMainFrameOnly:YES]];
        config.userContentController = controller;
    }
    _web = [[WKWebView alloc] initWithFrame:_window.contentView.bounds configuration:config];
    _web.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    _web.navigationDelegate = self;
    [_window.contentView addSubview:_web];
    [_window makeKeyAndOrderFront:nil];
    kShopWeb = _web;

    // Two settings belong to the shell, not to a page it shows, so they are picked up before the
    // page arrives: the size it reads at, and whether this Mac is held awake while it is open.
    double zoom = [[NSUserDefaults standardUserDefaults] doubleForKey:kZoomKey];
    _web.magnification = (zoom >= 0.9 && zoom <= 1.4) ? zoom : 1.0;
    if ([[NSUserDefaults standardUserDefaults] boolForKey:kStayAwakeKey]) holdAwakeNow(YES);

    // The shop's own Mac may already be serving the live book. Joining it is the difference between
    // this window showing the counter's book and showing the day this copy was built. A probe is the
    // build looking at its own work, so unless somebody names a shop to join it gets the copy it was
    // handed, never the shop's book — that is what keeps a check hermetic on the shop's own Mac.
    NSString *namedShop = [NSProcessInfo processInfo].environment[@"CHRISPHICS_SHOP_URL"];
    BOOL probing = (kProbeTo || kSettingsProbeTo) && namedShop.length == 0;
    BOOL ownOnly = [NSProcessInfo processInfo].environment[@"CHRISPHICS_OWN_BOOK"].boolValue;
    BOOL choseOwn = [[NSUserDefaults standardUserDefaults] boolForKey:kOwnBookKey];
    NSURL *live = (probing || ownOnly || choseOwn) ? nil : liveShop();
    NSString *joined = savedShopAddress();
    if (live) {
        kBase = live.absoluteString;
        kOnShopBook = YES;
        // Keep the address that answered, so tomorrow's launch — with the shop Mac asleep — stays on
        // the shop's book and lets the page's queue hold the work, rather than opening a second book.
        if (![joined isEqualToString:kBase]) rememberShop(kBase);
    } else if (!probing && !ownOnly && !choseOwn && joined.length) {
        // A Mac that has joined the shop stays on the shop's address even when the shop cannot be
        // reached right now. Starting its own book instead would put the day's work in a second
        // ledger, which is exactly what somebody would then have to bring in by hand.
        kBase = shopAt(joined).absoluteString ?: joined;
        kOnShopBook = YES;
    } else if (!engineUp(1.0)) {
        serving = startEngine();
    }
    [self showWhenReady];
}

- (void)openSettings {
    [ShopSettings show];
}

// The engine takes a second or two to open its book; the window asks again until it answers rather
// than painting a connection error at someone who did nothing wrong.
- (void)showWhenReady {
    // The shop's own page knows how to open from its cache while the shop is away; only an engine
    // started on this Mac needs the wait.
    if (kOnShopBook || engineUp(1.0) || ++_tries > 120) {
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
    if (kSettingsProbeTo) {
        NSString *folder = kSettingsProbeTo;
        kSettingsProbeTo = nil;
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.5 * NSEC_PER_SEC)),
                       dispatch_get_main_queue(), ^{
                           [ShopSettings probeInto:folder done:^{
                               [serving terminate];
                               exit(0);
                           }];
                       });
        return;
    }
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

// The page over the shop's loopback door arrives under the shop's own certificate. Accepting only
// that one keeps the padlock meaningful, and the shop's own address presented with anything else is
// still refused. A page's requests to any other host — the forecast service — are the system's
// judgement, as they are in a browser; refusing those silenced the weather on every installed Mac.
- (void)webView:(WKWebView *)view
    didReceiveAuthenticationChallenge:(NSURLAuthenticationChallenge *)challenge
      completionHandler:(void (^)(NSURLSessionAuthChallengeDisposition, NSURLCredential *))completionHandler {
    NSURLProtectionSpace *space = challenge.protectionSpace;
    if ([space.authenticationMethod isEqualToString:NSURLAuthenticationMethodServerTrust]) {
        if (shopChainTrusted(space.serverTrust, space.host)) {
            return completionHandler(NSURLSessionAuthChallengeUseCredential,
                                     [NSURLCredential credentialForTrust:space.serverTrust]);
        }
        if (shopOwnHost(space.host)) {
            return completionHandler(NSURLSessionAuthChallengeCancelAuthenticationChallenge, nil);
        }
        return completionHandler(NSURLSessionAuthChallengePerformDefaultHandling, nil);
    }
    completionHandler(NSURLSessionAuthChallengePerformDefaultHandling, nil);
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

// ------------------------------------------------------------- the menu's own work
// The menu bar is the same one the shop's Swift app has always had: the page is the same page, and a
// counter that learned ⌘N for a new job on one Mac should not find it missing on another. Everything
// here either asks the page to do the thing (window.chrisphics.act) or asks the engine the way the
// Settings pane already does, so no menu item is a promise the shell cannot keep.
- (void)runPage:(NSString *)js {
    if (kShopWeb) [kShopWeb evaluateJavaScript:js completionHandler:nil];
}

- (void)pageAct:(NSString *)name {
    [self runPage:[NSString stringWithFormat:@"window.chrisphics && window.chrisphics.act('%@')", name]];
}

- (void)newJob { [self pageAct:@"new-job"]; }
- (void)newQuote { [self pageAct:@"new-quote"]; }
- (void)newEnquiry { [self pageAct:@"new-lead"]; }
- (void)newClient { [self pageAct:@"new-client"]; }
- (void)receivePayment { [self pageAct:@"receive-payment"]; }
- (void)newExpense { [self pageAct:@"new-expense"]; }

- (void)reloadPage { [kShopWeb reload]; }

- (void)goPage:(NSString *)hash {
    [self runPage:[NSString stringWithFormat:@"window.location.hash='%@'", hash]];
}
- (void)goDashboard { [self goPage:@"#/dashboard"]; }
- (void)goJobs { [self goPage:@"#/jobs"]; }
- (void)goCollect { [self goPage:@"#/collect"]; }
- (void)goClients { [self goPage:@"#/clients"]; }
- (void)goLeads { [self goPage:@"#/leads"]; }
- (void)goAccounts { [self goPage:@"#/accounts"]; }
- (void)goExpenses { [self goPage:@"#/expenses"]; }
- (void)goReports { [self goPage:@"#/reports"]; }
- (void)goSpoiled { [self goPage:@"#/spoiled"]; }
- (void)goSync { [self goPage:@"#/sync"]; }
- (void)goShop { [self goPage:@"#/shop"]; }

- (void)undoEdit:(id)sender {
    if (kShopWeb) [kShopWeb.undoManager undo];
}
- (void)redoEdit:(id)sender {
    if (kShopWeb) [kShopWeb.undoManager redo];
}
- (void)closeWindow:(id)sender { [_window performClose:sender]; }
- (void)enterFullScreen:(id)sender { [_window toggleFullScreen:sender]; }

- (void)backupNow:(id)sender {
    __weak typeof(self) weak = self;
    callEngine(@"POST", @"api/backups", @{}, ^(NSDictionary *json, NSString *why) {
        NSAlert *answer = [[NSAlert alloc] init];
        answer.messageText = why ? @"The book was not copied"
                                 : @"The book was copied";
        answer.informativeText = why ?: [NSString stringWithFormat:@"%@ %@",
                                         json[@"file"] ?: @"A copy was made.",
                                         json[@"note"] ?: @""];
        [answer addButtonWithTitle:@"OK"];
        ShopWindow *strong = weak;
        if (strong) [answer beginSheetModalForWindow:strong->_window completionHandler:nil];
    });
}

- (void)revealBook:(id)sender {
    NSString *book = bookFile();
    NSFileManager *disk = [NSFileManager defaultManager];
    [[NSWorkspace sharedWorkspace] activateFileViewerSelectingURLs:
     @[[disk fileExistsAtPath:book] ? [NSURL fileURLWithPath:book]
                                       : [NSURL fileURLWithPath:supportFolder()]]];
}

- (void)openCopies:(id)sender {
    NSString *folder = copiesFolder();
    [[NSFileManager defaultManager] createDirectoryAtPath:folder withIntermediateDirectories:YES
                                                   attributes:nil error:nil];
    [[NSWorkspace sharedWorkspace] openURL:[NSURL fileURLWithPath:folder]];
}

- (NSMenuItem *)menuItem:(NSString *)title action:(SEL)action key:(NSString *)key {
    NSMenuItem *item = [[NSMenuItem alloc] initWithTitle:title action:action keyEquivalent:key];
    item.target = self;
    return item;
}

- (NSMenuItem *)menuItem:(NSString *)title action:(SEL)action
                     key:(NSString *)key mask:(NSEventModifierFlags)mask {
    NSMenuItem *item = [self menuItem:title action:action key:key];
    item.keyEquivalentModifierMask = mask;
    return item;
}

- (void)setMenu {
    NSMenu *main = [[NSMenu alloc] init];
    NSMenuItem *appItem = [[NSMenuItem alloc] init];
    [main addItem:appItem];
    NSMenu *appMenu = [[NSMenu alloc] init];
    [appMenu addItemWithTitle:[@"About " stringByAppendingString:kName]
                       action:@selector(orderFrontStandardAboutPanel:) keyEquivalent:@""];
    [appMenu addItem:[NSMenuItem separatorItem]];
    // Right under About, where a Mac user reaches for it: ⌘, opens the same window. The target is
    // named rather than left to the responder chain, so the item works from any window — including
    // no window at all — and the settings probe can prove the item itself is wired.
    NSMenuItem *settingsItem = [appMenu addItemWithTitle:@"Settings…" action:@selector(openSettings)
                                           keyEquivalent:@","];
    settingsItem.target = self;
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

    NSMenuItem *fileItem = [[NSMenuItem alloc] init];
    [main addItem:fileItem];
    NSMenu *file = [[NSMenu alloc] initWithTitle:@"File"];
    [file addItem:[self menuItem:@"New Job" action:@selector(newJob) key:@"n"]];
    [file addItem:[self menuItem:@"New Quote" action:@selector(newQuote) key:@"N"
                           mask:(NSEventModifierFlagCommand | NSEventModifierFlagShift)]];
    [file addItem:[self menuItem:@"New Enquiry" action:@selector(newEnquiry) key:@"i"]];
    [file addItem:[self menuItem:@"New Client" action:@selector(newClient) key:@"k"]];
    [file addItem:[self menuItem:@"Receive Payment" action:@selector(receivePayment) key:@"r"]];
    [file addItem:[self menuItem:@"Record Money Out" action:@selector(newExpense) key:@"e"]];
    [file addItem:[NSMenuItem separatorItem]];
    [file addItem:[self menuItem:@"Close Window" action:@selector(closeWindow:) key:@"w"]];
    fileItem.submenu = file;

    // Without an Edit menu the standard shortcuts do not reach the web view, and a cashier who cannot
    // paste a client's phone number into the counter reasonably calls that broken. Each item is
    // aimed at this shell rather than left to the responder chain: a nil target lets AppKit grey an
    // item out whenever the page holds no selection or no focused field, and a menu that goes grey
    // reads as broken to the person looking at it. The shell forwards to the web view, which knows
    // what Cut, Copy and Paste mean inside a page.
    NSMenuItem *editItem = [[NSMenuItem alloc] init];
    [main addItem:editItem];
    NSMenu *edit = [[NSMenu alloc] initWithTitle:@"Edit"];
    [edit addItem:[self menuItem:@"Undo" action:@selector(undoEdit:) key:@"z"]];
    [edit addItem:[self menuItem:@"Redo" action:@selector(redoEdit:) key:@"Z"
                           mask:(NSEventModifierFlagCommand | NSEventModifierFlagShift)]];
    [edit addItem:[NSMenuItem separatorItem]];
    NSMenuItem *cut = [edit addItemWithTitle:@"Cut" action:@selector(cutEdit:) keyEquivalent:@"x"];
    cut.target = self;
    NSMenuItem *copy = [edit addItemWithTitle:@"Copy" action:@selector(copyEdit:) keyEquivalent:@"c"];
    copy.target = self;
    NSMenuItem *paste = [edit addItemWithTitle:@"Paste" action:@selector(pasteEdit:) keyEquivalent:@"v"];
    paste.target = self;
    NSMenuItem *selectAll = [edit addItemWithTitle:@"Select All" action:@selector(selectAllEdit:)
                                     keyEquivalent:@"a"];
    selectAll.target = self;
    editItem.submenu = edit;

    NSMenuItem *viewItem = [[NSMenuItem alloc] init];
    [main addItem:viewItem];
    NSMenu *view = [[NSMenu alloc] initWithTitle:@"View"];
    [view addItem:[self menuItem:@"Reload" action:@selector(reloadPage) key:@"r"
                           mask:(NSEventModifierFlagCommand | NSEventModifierFlagShift)]];
    [view addItem:[NSMenuItem separatorItem]];
    [view addItem:[self menuItem:@"Dashboard" action:@selector(goDashboard) key:@"1"]];
    [view addItem:[self menuItem:@"Print Jobs" action:@selector(goJobs) key:@"2"]];
    /* The counter's screen, so it sits beside the order book. It has no number key: ⌘1…⌘0 are
       already spoken for, and a hand-over is one click from the rail anyway. */
    [view addItem:[self menuItem:@"Collect" action:@selector(goCollect) key:@""]];
    [view addItem:[self menuItem:@"Clients" action:@selector(goClients) key:@"3"]];
    [view addItem:[self menuItem:@"Enquiries" action:@selector(goLeads) key:@"4"]];
    [view addItem:[self menuItem:@"Accounts" action:@selector(goAccounts) key:@"5"]];
    [view addItem:[self menuItem:@"Expenses" action:@selector(goExpenses) key:@"6"]];
    [view addItem:[self menuItem:@"Reports" action:@selector(goReports) key:@"7"]];
    [view addItem:[self menuItem:@"Spoiled Work" action:@selector(goSpoiled) key:@"8"]];
    [view addItem:[self menuItem:@"Pending Sync" action:@selector(goSync) key:@"9"]];
    [view addItem:[self menuItem:@"Shop & Devices" action:@selector(goShop) key:@"0"]];
    [view addItem:[NSMenuItem separatorItem]];
    [view addItem:[self menuItem:@"Enter Full Screen" action:@selector(enterFullScreen:) key:@"f"
                           mask:(NSEventModifierFlagControl | NSEventModifierFlagCommand)]];
    viewItem.submenu = view;

    NSMenuItem *dataItem = [[NSMenuItem alloc] init];
    [main addItem:dataItem];
    NSMenu *data = [[NSMenu alloc] initWithTitle:@"Data"];
    [data addItem:[self menuItem:@"Back Up the Book Now" action:@selector(backupNow:) key:@"b"]];
    [data addItem:[self menuItem:@"Show the Book in Finder" action:@selector(revealBook:) key:@""]];
    [data addItem:[self menuItem:@"Open the Backups Folder" action:@selector(openCopies:) key:@""]];
    dataItem.submenu = data;

    NSMenuItem *windowItem = [[NSMenuItem alloc] init];
    [main addItem:windowItem];
    NSMenu *windows = [[NSMenu alloc] initWithTitle:@"Window"];
    [windows addItemWithTitle:@"Minimise" action:@selector(miniaturize:) keyEquivalent:@"m"];
    [windows addItemWithTitle:@"Zoom" action:@selector(zoom:) keyEquivalent:@""];
    windowItem.submenu = windows;

    NSApp.mainMenu = main;
    NSApp.windowsMenu = windows;
}

// The Edit menu's items are aimed here so AppKit never greys them; the web view itself knows what
// each one means inside a page, so the shell just hands them on. WKWebView does not declare these
// selectors, so they go through performSelector — the same road the shop's own Mac app takes.
- (void)cutEdit:(id)sender {
    if ([kShopWeb respondsToSelector:@selector(cut:)]) [kShopWeb performSelector:@selector(cut:) withObject:sender];
}
- (void)copyEdit:(id)sender {
    if ([kShopWeb respondsToSelector:@selector(copy:)]) [kShopWeb performSelector:@selector(copy:) withObject:sender];
}
- (void)pasteEdit:(id)sender {
    if ([kShopWeb respondsToSelector:@selector(paste:)]) [kShopWeb performSelector:@selector(paste:) withObject:sender];
}
- (void)selectAllEdit:(id)sender {
    if ([kShopWeb respondsToSelector:@selector(selectAll:)]) [kShopWeb performSelector:@selector(selectAll:) withObject:sender];
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

// ------------------------------------------------------------ settings
// Everything a person changes about this copy without leaving it: where its book and copies are,
// how big the page reads, whether it opens by itself and stays awake, the paper behind every
// screen, the shop's own words that go on every sheet, and which build this is. A window with
// four panes rather than a web screen, because three of the four are about the shell itself —
// the half a page cannot reach.
static NSString *const kRepository = @"https://github.com/HowellDaniel/ChrisphicsHub";
static NSString *const kLaunchLabel = @"com.crispprint.chrisphicshub.portable";

// caffeinate holds this Mac awake for as long as this process lives, so quitting the app is all it
// takes to let the Mac sleep again — the same arrangement the engine uses when it serves a Wi-Fi.
static void holdAwakeNow(BOOL on) {
    if (on && !keepingAwake) {
        NSTask *task = [[NSTask alloc] init];
        task.executableURL = [NSURL fileURLWithPath:@"/usr/bin/caffeinate"];
        task.arguments = @[@"-i", @"-s", @"-w", [NSString stringWithFormat:@"%d", getpid()]];
        if ([task launchAndReturnError:NULL]) keepingAwake = task;
    } else if (!on && keepingAwake) {
        [keepingAwake terminate];
        keepingAwake = nil;
    }
}

static NSString *bookFile(void) {
    return [supportFolder() stringByAppendingPathComponent:@"shop.db"];
}

static NSString *copiesFolder(void) {
    return [supportFolder() stringByAppendingPathComponent:@"Backups"];
}

// The home this copy is running under, written as `~`. Not `stringByAbbreviatingWithTildeInPath`:
// that asks the user database, while this app deliberately follows HOME so a copy run under another
// home — as the build check does — reads and writes there and nowhere else.
static NSString *shortPath(NSString *path) {
    NSString *home = [NSProcessInfo processInfo].environment[@"HOME"];
    if (home.length && [path hasPrefix:home])
        return [@"~" stringByAppendingString:[path substringFromIndex:home.length]];
    return [path stringByAbbreviatingWithTildeInPath];
}

static NSTextField *captionText(NSString *text) {
    NSTextField *label = [NSTextField labelWithString:text ?: @""];
    label.textColor = [NSColor labelColor];
    return label;
}

// The right-hand half of a form row: what the shop is set to, not something to type in.
static NSTextField *valueText(NSString *text) {
    NSTextField *label = [NSTextField labelWithString:text ?: @""];
    label.textColor = [NSColor secondaryLabelColor];
    label.font = [NSFont systemFontOfSize:12];
    label.selectable = YES;
    label.lineBreakMode = NSLineBreakByTruncatingMiddle;
    [label.widthAnchor constraintGreaterThanOrEqualToConstant:250].active = YES;
    [label.widthAnchor constraintLessThanOrEqualToConstant:320].active = YES;
    return label;
}

static NSTextField *entryField(NSString *placeholder) {
    NSTextField *field = [NSTextField textFieldWithString:@""];
    field.placeholderString = placeholder;
    [field.widthAnchor constraintEqualToConstant:300].active = YES;
    return field;
}

static NSTextField *statusText(void) {
    NSTextField *line = [NSTextField labelWithString:@""];
    line.textColor = [NSColor secondaryLabelColor];
    line.font = [NSFont systemFontOfSize:12];
    line.lineBreakMode = NSLineBreakByWordWrapping;
    line.maximumNumberOfLines = 4;
    line.preferredMaxLayoutWidth = 470;
    return line;
}

static NSTextField *noteText(NSString *text) {
    NSTextField *line = [NSTextField labelWithString:text ?: @""];
    line.textColor = [NSColor secondaryLabelColor];
    line.font = [NSFont systemFontOfSize:11];
    line.lineBreakMode = NSLineBreakByWordWrapping;
    line.maximumNumberOfLines = 5;
    line.preferredMaxLayoutWidth = 470;
    return line;
}

static NSButton *pushButton(NSString *title, id target, SEL action) {
    NSButton *button = [NSButton buttonWithTitle:title target:target action:action];
    button.bezelStyle = NSBezelStyleRounded;
    return button;
}

static NSStackView *rowOf(NSArray<NSView *> *views) {
    NSStackView *row = [NSStackView stackViewWithViews:views];
    row.orientation = NSUserInterfaceLayoutOrientationHorizontal;
    row.spacing = 8;
    row.alignment = NSLayoutAttributeCenterY;
    return row;
}

// A label/control grid, with any number of full-width lines below it. Same shape as the shop's own
// Mac app, so the two settings windows read alike.
static NSGridView *formGrid(NSArray<NSArray<NSView *> *> *rows, NSArray<NSView *> *notes) {
    NSGridView *grid = [NSGridView gridViewWithViews:rows];
    grid.rowSpacing = 10;
    grid.columnSpacing = 12;
    grid.translatesAutoresizingMaskIntoConstraints = NO;
    if (grid.numberOfColumns > 1) {
        [grid columnAtIndex:0].xPlacement = NSGridCellPlacementTrailing;
        [grid columnAtIndex:1].xPlacement = NSGridCellPlacementLeading;
    }
    for (NSView *note in notes) {
        NSGridRow *row = [grid addRowWithViews:@[note, [NSGridCell emptyContentView]]];
        [row mergeCellsInRange:NSMakeRange(0, grid.numberOfColumns)];
        [row cellAtIndex:0].xPlacement = NSGridCellPlacementLeading;
        row.topPadding = 4;
    }
    return grid;
}

// ------------------------------------------------------------- the engine's own answers
static void callEngine(NSString *method, NSString *path, NSDictionary *body,
                       void (^done)(NSDictionary *json, NSString *why)) {
    NSMutableURLRequest *request = [NSMutableURLRequest requestWithURL:
                                   [NSURL URLWithString:urlPath(path)]];
    request.HTTPMethod = method;
    request.timeoutInterval = 10;
    if (body) {
        request.HTTPBody = [NSJSONSerialization dataWithJSONObject:body options:0 error:nil];
        [request setValue:@"application/json" forHTTPHeaderField:@"Content-Type"];
    }
    NSURLSession *session = shopSession();
    [[session dataTaskWithRequest:request
               completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
                   NSDictionary *json = data.length ? [NSJSONSerialization JSONObjectWithData:data
                                                                                      options:0
                                                                                        error:nil] : nil;
                   NSInteger status = [(NSHTTPURLResponse *)response statusCode];
                   NSString *why = nil;
                   if (error) why = error.localizedDescription;
                   else if (status >= 400) why = json[@"error"]
                       ?: [NSString stringWithFormat:@"the engine answered %ld", (long)status];
                   dispatch_async(dispatch_get_main_queue(), ^{ done(json, why); });
               }] resume];
}

// ------------------------------------------------------------- the panes
@interface SettingsPane : NSViewController
@property (nonatomic, copy) NSString *paneName;
// The symbol beside the pane's name in the toolbar, the same four the shop's own Mac app shows.
@property (nonatomic, copy) NSString *paneSymbol;
@property (nonatomic, strong) NSTextField *status;
- (void)say:(NSString *)text colour:(NSColor *)colour;
- (void)place:(NSGridView *)grid;
@end

@implementation SettingsPane
- (NSString *)paneSymbol { return _paneSymbol.length ? _paneSymbol : @"gearshape"; }
- (void)say:(NSString *)text colour:(NSColor *)colour {
    self.status.stringValue = text ?: @"";
    self.status.textColor = colour ?: [NSColor secondaryLabelColor];
}

- (void)place:(NSGridView *)grid {
    NSView *root = [[NSView alloc] initWithFrame:NSMakeRect(0, 0, 560, 380)];
    [root addSubview:grid];
    // Only the top edge is pinned. Pinning the bottom too makes the grid stretch to whatever height
    // the window happens to be, and the notes — the last rows — drift away from the form they belong
    // to, which reads as a form with a hole in it.
    [NSLayoutConstraint activateConstraints:@[
        [grid.topAnchor constraintEqualToAnchor:root.topAnchor constant:22],
        [grid.centerXAnchor constraintEqualToAnchor:root.centerXAnchor],
        [grid.leadingAnchor constraintGreaterThanOrEqualToAnchor:root.leadingAnchor constant:20],
        [root.widthAnchor constraintGreaterThanOrEqualToConstant:560],
        [root.heightAnchor constraintGreaterThanOrEqualToConstant:380],
    ]];
    self.view = root;
}
@end

// -- General: which book is shown, copies, page size, staying awake and opening at sign-in
@interface SettingsGeneral : SettingsPane
@end

// A copy of the book that is whole. `.backup` is the online backup the engine itself uses; a file
// simply read while the engine has it open can be missing the newest commits still in the
// write-ahead log, and those are exactly the entries somebody wants brought in.
static NSString *wholeCopyOfBook(NSString *book) {
    NSString *temp = [NSTemporaryDirectory() stringByAppendingPathComponent:
                      [NSString stringWithFormat:@"chrisphics-bring-%d.db", (int)getpid()]];
    [[NSFileManager defaultManager] removeItemAtPath:temp error:nil];
    NSTask *task = [[NSTask alloc] init];
    task.executableURL = [NSURL fileURLWithPath:@"/usr/bin/sqlite3"];
    task.arguments = @[book, [NSString stringWithFormat:@".backup '%@'", temp]];
    task.standardOutput = [NSFileHandle fileHandleWithNullDevice];
    task.standardError = [NSFileHandle fileHandleWithNullDevice];
    if (![task launchAndReturnError:NULL]) return nil;
    [task waitUntilExit];
    if (task.terminationStatus != 0 || ![[NSFileManager defaultManager] fileExistsAtPath:temp])
        return nil;
    return temp;
}

@implementation SettingsGeneral {
    NSTextField *_engineValue;
    NSTextField *_bookValue;
    NSTextField *_copiesValue;
    NSTextField *_zoomValue;
    NSTextField *_whereValue;
    NSTextField *_bookNote;
    NSButton *_awakeBox;
    NSButton *_joinButton;
    NSButton *_ownButton;
    NSButton *_bringButton;
}

- (instancetype)init {
    if ((self = [super init])) {
        self.paneName = @"General";
        self.paneSymbol = @"gearshape";
    }
    return self;
}

static NSString *sizeWords(unsigned long long bytes) {
    if (bytes >= 1048576) return [NSString stringWithFormat:@"%.1f MB", bytes / 1048576.0];
    return [NSString stringWithFormat:@"%llu KB", (bytes + 1023) / 1024];
}

- (void)loadView {
    _engineValue = valueText(kBase);
    NSString *book = bookFile();
    unsigned long long bytes = [[[NSFileManager defaultManager]
                                 attributesOfItemAtPath:book error:nil][NSFileSize] unsignedLongLongValue];
    _bookValue = valueText(bytes ? [NSString stringWithFormat:@"%@ · %@",
                                    shortPath(book), sizeWords(bytes)]
                                 : shortPath(book));
    _bookValue.toolTip = book;
    _copiesValue = valueText(shortPath(copiesFolder()));
    _copiesValue.toolTip = copiesFolder();

    // Which book this window is showing, and the one action that changes it. A copy that has joined
    // the shop shows where the shop is; its own book is still on the disk, untouched, and can be
    // brought into the shop's book on the day it joins.
    _whereValue = valueText(@"");
    _joinButton = pushButton(@"Join the shop's book", self, @selector(joinTheShop));
    _ownButton = pushButton(@"Use this Mac's own book", self, @selector(comeBackToThisMac));
    _bringButton = pushButton(@"Bring this Mac's entries in…", self, @selector(bringThisMacIn));

    NSButton *look = pushButton(@"Open in browser", self, @selector(openInBrowser));
    NSButton *reveal = pushButton(@"Show in Finder", self, @selector(revealBook));
    NSButton *openCopies = pushButton(@"Open folder", self, @selector(openCopies));
    NSButton *copyNow = pushButton(@"Copy the book now", self, @selector(copyNow));

    // Page size is the whole page, not only the words: a cashier on a small screen wants the
    // figures bigger, and a settings window that quietly changed one and not the other would be
    // lying about what it did.
    double zoom = [[NSUserDefaults standardUserDefaults] doubleForKey:kZoomKey];
    if (zoom < 0.9 || zoom > 1.4) zoom = 1.0;
    NSSlider *slider = [NSSlider sliderWithValue:zoom minValue:0.9 maxValue:1.4
                                          target:self action:@selector(moveZoom:)];
    [slider.widthAnchor constraintEqualToConstant:190].active = YES;
    _zoomValue = valueText([NSString stringWithFormat:@"%d%%", (int)llround(zoom * 100)]);
    [_zoomValue.widthAnchor constraintLessThanOrEqualToConstant:48].active = YES;

    _awakeBox = [NSButton checkboxWithTitle:@"Keep this Mac awake while the shop is open"
                                     target:self action:@selector(toggleAwake:)];
    _awakeBox.state = [[NSUserDefaults standardUserDefaults] boolForKey:kStayAwakeKey]
        ? NSControlStateValueOn : NSControlStateValueOff;

    NSButton *login = [NSButton checkboxWithTitle:
                       [NSString stringWithFormat:@"Open %@ when I sign in to this Mac", kName]
                                           target:self action:@selector(toggleLogin:)];
    login.state = [self loginItemThere] ? NSControlStateValueOn : NSControlStateValueOff;

    self.status = statusText();
    _bookNote = noteText(@"");
    NSGridView *grid = formGrid(@[@[captionText(@"Records engine:"), rowOf(@[_engineValue, pushButton(@"Refresh", self, @selector(refreshEngine))])],
                                   @[captionText(@"Book shown:"), rowOf(@[_whereValue, _joinButton, _ownButton])],
                                   @[captionText(@"Entries from this Mac:"), _bringButton],
                                   @[captionText(@"Open the shop:"), look],
                                   @[captionText(@"This computer's book:"), rowOf(@[_bookValue, reveal])],
                                   @[captionText(@"Copies:"), rowOf(@[_copiesValue, openCopies, copyNow])],
                                   @[captionText(@"Page size:"), rowOf(@[slider, _zoomValue])],
                                   @[captionText(@"Power:"), _awakeBox],
                                   @[captionText(@"Sign-in:"), login]],
                                 @[noteText(@"The book is the one file holding every job, client and payment. "
                                            "A copy is made to the copies folder by itself each day, and one "
                                            "can be asked for at any time."),
                                   _bookNote,
                                   self.status]);
    [self place:grid];
    [self refreshBookRows];
}

// What the two book rows say, and which of their buttons makes sense at this moment. On the shop
// Mac itself there is nothing to join and nothing to bring in — the book it keeps is the shop's.
- (void)refreshBookRows {
    BOOL shop = kOnShopBook;
    BOOL here = shop && onThisMac([[NSURL URLWithString:kBase] host] ?: @"");
    _whereValue.stringValue = shop
        ? [NSString stringWithFormat:@"The shop's one book · %@", kBase]
        : [NSString stringWithFormat:@"This Mac's own book · %@", shortPath(bookFile())];
    _joinButton.hidden = shop;
    _ownButton.hidden = !shop || here;

    NSNumber *size = [[[NSFileManager defaultManager] attributesOfItemAtPath:bookFile()
                                                                      error:nil] objectForKey:NSFileSize];
    BOOL hasOwn = size.unsignedLongLongValue > 8192;
    _bringButton.enabled = hasOwn && !here;
    _bringButton.toolTip = here ? @"This Mac is the shop: its book is already the shop's book."
        : !hasOwn ? @"This Mac has not kept a book of its own yet."
        : @"Adds what this Mac's book holds to the shop's book. Nothing on this Mac is changed.";

    _bookNote.stringValue = here
        ? @"This window is showing the shop's live book on this Mac, so anything entered here is "
           "written to it. The copy this computer keeps is left as it was."
        : shop
        ? @"This window shows the shop's one book over the shop's Wi-Fi. Anything entered here is "
           "written to it and appears on every other Mac. When the shop cannot be reached, what is "
           "typed is held on this Mac and goes in when it answers again."
        : @"Only this computer's own records live here. The shop's shared book stays on the shop's "
           "Mac; joining it puts this window on that book, and bringing this Mac's entries in adds "
           "what it holds without changing anything here.";
}

- (void)viewDidAppear {
    [super viewDidAppear];
    [self describeCopies];
}

// How many copies are already kept, and how much room the disk has for more: the two facts a person
// looking at the copies folder actually wants. The count rides beside the folder; the newest copy
// and the free space go in the line that would otherwise sit empty, and never talk over a copy the
// person has just asked for.
- (void)describeCopies {
    __weak typeof(self) weak = self;
    callEngine(@"GET", @"api/backups", nil, ^(NSDictionary *json, NSString *why) {
        SettingsGeneral *pane = weak;
        if (!pane || why || !json) return;
        NSNumber *count = [json[@"copies"] isKindOfClass:[NSNumber class]] ? json[@"copies"] : @0;
        pane->_copiesValue.stringValue = [NSString stringWithFormat:@"%@ · %@ cop%@",
                                          shortPath(copiesFolder()), count,
                                          count.integerValue == 1 ? @"y" : @"ies"];
        if (pane.status.stringValue.length) return;
        NSMutableString *words = [NSMutableString string];
        NSString *newest = [json[@"file"] isKindOfClass:[NSString class]] ? json[@"file"] : nil;
        if (newest.length) [words appendFormat:@"Newest %@.", newest];
        NSNumber *room = [json[@"free_gb"] isKindOfClass:[NSNumber class]] ? json[@"free_gb"] : nil;
        if (room) [words appendFormat:@" %@ GB free there.", room];
        if (words.length) [pane say:words colour:nil];
    });
}

// ------------------------------------------------------------ joining, leaving, bringing in

- (void)joinTheShop {
    [self say:@"Looking for the shop…" colour:nil];
    __weak typeof(self) weak = self;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        NSURL *shop = liveShop();
        dispatch_async(dispatch_get_main_queue(), ^{
            SettingsGeneral *pane = weak;
            if (!pane) return;
            if (!shop) {
                [pane say:@"The shop's book is not answering. Check that the shop Mac is awake and "
                           "that this Mac is on the shop's Wi-Fi."
                   colour:[NSColor systemRedColor]];
                return;
            }
            joinShop(shop.absoluteString);
            pane->_engineValue.stringValue = kBase;
            [pane refreshBookRows];
            [pane say:[NSString stringWithFormat:@"This window now shows the shop's book at %@.",
                       shop.absoluteString] colour:[NSColor systemGreenColor]];
        });
    });
}

- (void)comeBackToThisMac {
    NSAlert *ask = [[NSAlert alloc] init];
    ask.messageText = @"Show this Mac's own book again?";
    ask.informativeText = @"Anything entered after this is written here, not to the shop's shared "
                           "book, and will not appear on the other Macs. Entries already in the "
                           "shop's book stay there.";
    [ask addButtonWithTitle:@"Use this Mac's own book"];
    [ask addButtonWithTitle:@"Cancel"];
    if ([ask runModal] != NSAlertFirstButtonReturn) return;
    useOwnBook();
    [self refreshBookRows];
    [self say:@"This window is back on this Mac's own book." colour:nil];
}

- (void)bringThisMacIn {
    NSNumber *size = [[[NSFileManager defaultManager] attributesOfItemAtPath:bookFile()
                                                                      error:nil]
                      objectForKey:NSFileSize];
    if (size.unsignedLongLongValue <= 8192) {
        [self say:@"This Mac has not kept a book of its own, so there is nothing to bring in."
           colour:[NSColor systemRedColor]];
        return;
    }
    NSAlert *ask = [[NSAlert alloc] init];
    ask.messageText = @"Bring this Mac's entries into the shop's book?";
    ask.informativeText = @"The book on this Mac is read, and every update in it reaches the shop's "
                           "book: new clients (matched by phone), jobs, payments, expenses, spoiled "
                           "work, enquiries and the history that goes with them, and a job this Mac "
                           "changed after the shop did replaces the shop's older copy of that job, "
                           "lines included. An older copy never replaces a newer one. A job number "
                           "is changed only when the shop has already issued it. A copy of the "
                           "shop's book is taken first, and this Mac's own book is not changed."
                           "\n\nIf anything here is marked \"Held on this device\", let it reach "
                           "this Mac's book before bringing it in.";
    [ask addButtonWithTitle:@"Bring them in"];
    [ask addButtonWithTitle:@"Cancel"];
    if ([ask runModal] != NSAlertFirstButtonReturn) return;

    [self say:@"Reading this Mac's book…" colour:nil];
    __weak typeof(self) weak = self;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        NSURL *shop = liveShop();
        NSString *snapshot = wholeCopyOfBook(bookFile());
        NSData *raw = snapshot ? [NSData dataWithContentsOfFile:snapshot]
                               : [NSData dataWithContentsOfFile:bookFile()];
        if (snapshot) [[NSFileManager defaultManager] removeItemAtPath:snapshot error:nil];
        if (!shop) {
            dispatch_async(dispatch_get_main_queue(), ^{
                [weak say:@"The shop's book is not answering, so nothing was brought in. Check that "
                          @"the shop Mac is awake and that this Mac is on the shop's Wi-Fi."
                    colour:[NSColor systemRedColor]];
            });
            return;
        }
        if (raw.length < 4096) {
            dispatch_async(dispatch_get_main_queue(), ^{
                [weak say:@"This Mac's book could not be read, so nothing was brought in."
                    colour:[NSColor systemRedColor]];
            });
            return;
        }
        NSString *label = [[[NSProcessInfo processInfo] hostName]
                           stringByReplacingOccurrencesOfString:@".local" withString:@""];
        NSString *query = [label stringByAddingPercentEncodingWithAllowedCharacters:
                           [NSCharacterSet URLQueryAllowedCharacterSet]] ?: @"";
        NSMutableURLRequest *request = [NSMutableURLRequest requestWithURL:
            [NSURL URLWithString:[@"api/bring-over?label=" stringByAppendingString:query]
                    relativeToURL:shop]];
        request.HTTPMethod = @"POST";
        request.timeoutInterval = 120;
        request.HTTPBody = raw;
        [request setValue:@"application/x-sqlite3" forHTTPHeaderField:@"Content-Type"];
        [[shopSession() dataTaskWithRequest:request
                          completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
            NSDictionary *json = data.length ? [NSJSONSerialization JSONObjectWithData:data
                                                                              options:0
                                                                                error:nil] : nil;
            NSInteger status = [(NSHTTPURLResponse *)response statusCode];
            dispatch_async(dispatch_get_main_queue(), ^{
                SettingsGeneral *pane = weak;
                if (!pane) return;
                if (error || status >= 400) {
                    NSString *why = json[@"error"] ?: error.localizedDescription
                        ?: [NSString stringWithFormat:@"the shop answered %ld", (long)status];
                    [pane say:why colour:[NSColor systemRedColor]];
                    return;
                }
                joinShop(shop.absoluteString);
                pane->_engineValue.stringValue = kBase;
                [pane refreshBookRows];
                NSDictionary *records = [json[@"records"] isKindOfClass:[NSDictionary class]]
                    ? json[@"records"] : @{};
                NSDictionary *added = [records[@"added"] isKindOfClass:[NSDictionary class]]
                    ? records[@"added"] : @{};
                NSDictionary *updated = [records[@"updated"] isKindOfClass:[NSDictionary class]]
                    ? records[@"updated"] : @{};
                NSArray *renumbered = [records[@"renumbered"] isKindOfClass:[NSArray class]]
                    ? records[@"renumbered"] : @[];
                if ([json[@"already_brought_in"] boolValue]) {
                    [pane say:[NSString stringWithFormat:
                               @"This copy of the book was already brought in on %@, so nothing "
                               @"was added again. This window now shows the shop's book.",
                               json[@"at"] ?: @"an earlier day"]
                       colour:[NSColor systemGreenColor]];
                    return;
                }
                NSDictionary *nouns = @{@"clients": @"client", @"jobs": @"job",
                                        @"job_items": @"line", @"payments": @"payment",
                                        @"expenses": @"expense", @"spoiled_work": @"spoiled record",
                                        @"leads": @"enquiry", @"job_events": @"history note",
                                        @"notifications": @"message"};
                NSMutableArray *parts = [NSMutableArray array];
                for (NSString *key in [added.allKeys sortedArrayUsingSelector:@selector(compare:)]) {
                    NSInteger count = [added[key] integerValue];
                    if (count) [parts addObject:[NSString stringWithFormat:@"%ld %@%@", (long)count,
                                                 nouns[key] ?: key, count == 1 ? @"" : @"s"]];
                }
                NSMutableArray *fresh = [NSMutableArray array];
                for (NSString *key in [updated.allKeys sortedArrayUsingSelector:@selector(compare:)]) {
                    NSInteger count = [updated[key] integerValue];
                    if (count) [fresh addObject:[NSString stringWithFormat:@"%ld %@%@", (long)count,
                                                 nouns[key] ?: key, count == 1 ? @"" : @"s"]];
                }
                NSMutableString *words = [NSMutableString string];
                if (parts.count) [words appendFormat:@"Brought in %@ from this Mac.",
                                  [parts componentsJoinedByString:@", "]];
                if (fresh.count) {
                    if (words.length) [words appendString:@" "];
                    [words appendFormat:@"Brought %@ up to date from this Mac.",
                     [fresh componentsJoinedByString:@", "]];
                }
                if (!words.length) [words appendString:@"Nothing on this Mac was newer than the "
                                                    @"shop's book."];
                if (renumbered.count) {
                    NSDictionary *first = renumbered.firstObject;
                    [words appendFormat:@" %lu clashing job number%@ renumbered (the first: %@ → %@).",
                     (unsigned long)renumbered.count, renumbered.count == 1 ? @"" : @"s",
                     first[@"from"] ?: @"?", first[@"to"] ?: @"?"];
                }
                [words appendString:@" This window now shows the shop's book."];
                [pane say:words colour:[NSColor systemGreenColor]];
            });
        }] resume];
    });
}

- (BOOL)loginItemThere {
    return [[NSFileManager defaultManager] fileExistsAtPath:[self loginPath]];
}

- (NSString *)loginPath {
    return [NSHomeDirectory() stringByAppendingPathComponent:
            [@"Library/LaunchAgents" stringByAppendingPathComponent:
             [kLaunchLabel stringByAppendingString:@".plist"]]];
}

- (void)openInBrowser {
    [[NSWorkspace sharedWorkspace] openURL:[NSURL URLWithString:kBase]];
}

- (void)revealBook {
    NSString *book = bookFile();
    NSFileManager *disk = [NSFileManager defaultManager];
    [[NSWorkspace sharedWorkspace] activateFileViewerSelectingURLs:
     @[[disk fileExistsAtPath:book] ? [NSURL fileURLWithPath:book]
                                       : [NSURL fileURLWithPath:supportFolder()]]];
}

- (void)openCopies {
    NSString *folder = copiesFolder();
    [[NSFileManager defaultManager] createDirectoryAtPath:folder withIntermediateDirectories:YES
                                                   attributes:nil error:nil];
    [[NSWorkspace sharedWorkspace] openURL:[NSURL fileURLWithPath:folder]];
}

- (void)refreshEngine {
    [self say:@"Asking the engine…" colour:nil];
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        BOOL up = engineUp(2.0);
        dispatch_async(dispatch_get_main_queue(), ^{
            self->_engineValue.stringValue = up ? kBase
                : [kBase stringByAppendingString:@" — not answering"];
            [self say:(up ? @"The engine is answering on this computer."
                         : @"The engine is not answering on this port.")
               colour:[NSColor secondaryLabelColor]];
        });
    });
}

- (void)copyNow {
    [self say:@"Copying the book…" colour:nil];
    __weak typeof(self) weak = self;
    callEngine(@"POST", @"api/backups", @{}, ^(NSDictionary *json, NSString *why) {
        if (why) { [weak say:why colour:[NSColor systemRedColor]]; return; }
        [weak say:[NSString stringWithFormat:@"Copied %@ — %@", json[@"file"] ?: @"the book",
                   json[@"note"] ?: @""]
            colour:[NSColor systemGreenColor]];
        [weak describeCopies];
    });
}

- (void)moveZoom:(NSSlider *)sender {
    double zoom = sender.doubleValue;
    _zoomValue.stringValue = [NSString stringWithFormat:@"%d%%", (int)llround(zoom * 100)];
    [[NSUserDefaults standardUserDefaults] setDouble:zoom forKey:kZoomKey];
    kShopWeb.magnification = zoom;
    [self say:@"The page will read at this size every time the shop opens." colour:nil];
}

- (void)toggleAwake:(NSButton *)sender {
    BOOL want = sender.state == NSControlStateValueOn;
    [[NSUserDefaults standardUserDefaults] setBool:want forKey:kStayAwakeKey];
    holdAwakeNow(want);
    [self say:want ? @"This Mac will stay awake while the shop is open."
                   : @"This Mac may sleep when it is left alone." colour:nil];
}

// The sign-in item is one file the user can read, and taking the tick out removes it. Nothing is
// loaded into the running session: the point is the next sign-in, and a launchd job started now
// would open a second window for somebody who did not ask for one.
- (void)toggleLogin:(NSButton *)sender {
    BOOL want = sender.state == NSControlStateValueOn;
    NSFileManager *disk = [NSFileManager defaultManager];
    NSString *path = [self loginPath];
    if (!want) {
        [disk removeItemAtPath:path error:nil];
        [self say:@"The app will not open by itself." colour:nil];
        return;
    }
    NSString *exe = [NSBundle mainBundle].executablePath;
    if (!exe.length) {
        sender.state = NSControlStateValueOff;
        [self say:@"This copy has no program of its own to open at sign-in."
            colour:[NSColor systemRedColor]];
        return;
    }
    [disk createDirectoryAtPath:[path stringByDeletingLastPathComponent]
    withIntermediateDirectories:YES attributes:nil error:nil];
    NSDictionary *plist = @{@"Label": kLaunchLabel,
                            @"ProgramArguments": @[exe],
                            @"RunAtLoad": @YES,
                            @"LimitLoadToSessionType": @"Aqua"};
    NSData *data = [NSPropertyListSerialization dataWithPropertyList:plist
                                                              format:NSPropertyListXMLFormat_v1_0
                                                             options:0 error:nil];
    if (!data || ![data writeToFile:path atomically:YES]) {
        sender.state = NSControlStateValueOff;
        [self say:@"That sign-in file could not be written." colour:[NSColor systemRedColor]];
        return;
    }
    [self say:@"The app will open when you next sign in to this Mac." colour:nil];
}
@end

// -- Appearance: the paper behind every screen, picked here and kept on this computer
@interface SettingsAppearance : SettingsPane
@end

@implementation SettingsAppearance {
    NSPopUpButton *_pop;
}

- (instancetype)init {
    if ((self = [super init])) {
        self.paneName = @"Appearance";
        self.paneSymbol = @"paintpalette";
    }
    return self;
}

- (void)loadView {
    _pop = [[NSPopUpButton alloc] initWithFrame:NSZeroRect pullsDown:NO];
    for (NSArray<NSString *> *choice in backgroundChoices())
        [_pop addItemWithTitle:choice[1]];
    NSArray<NSArray<NSString *> *> *choices = backgroundChoices();
    NSString *now = chosenBackground();
    NSInteger hit = 0;
    for (NSInteger i = 0; i < (NSInteger)choices.count; i++)
        if ([choices[i][0] isEqualToString:now]) hit = i;
    [_pop selectItemAtIndex:hit];
    _pop.target = self;
    _pop.action = @selector(pick);

    self.status = statusText();
    NSGridView *grid = formGrid(@[ @[ captionText(@"Background:"), _pop ] ],
                                @[ noteText(@"The paper behind every screen, and the pattern on it. "
                                            "The clock still owns day and night — this only repaints what the "
                                            "clock lights, and it stays on this Mac's copy of the page. The job "
                                            "sheet a client signs always keeps the white paper it must be."),
                                   self.status ]);
    [self place:grid];
}

- (void)pick {
    NSArray<NSArray<NSString *> *> *choices = backgroundChoices();
    NSInteger hit = _pop.indexOfSelectedItem;
    if (hit < 0 || hit >= (NSInteger)choices.count) return;
    NSString *key = choices[hit][0];
    [[NSUserDefaults standardUserDefaults] setObject:key forKey:kBackgroundKey];
    if (kShopWeb) {
        [kShopWeb evaluateJavaScript:[NSString stringWithFormat:
            @"document.documentElement.dataset.bg='%@';", key] completionHandler:nil];
    }
    [self say:[key isEqualToString:@"plain"]
             ? @"The page is back on the shop's own paper."
             : [NSString stringWithFormat:@"The page now wears the %@ look.", choices[hit][1]]
        colour:nil];
}
@end

// -- Profile: the shop's own words, kept in the book so every sheet and every device agrees
@interface SettingsProfile : SettingsPane
@end

@implementation SettingsProfile {
    NSTextField *_name;
    NSTextField *_tagline;
    NSTextField *_phone;
    NSTextField *_address;
    NSTextField *_currency;
    NSTextField *_momo;
    NSTextField *_install;
}

- (instancetype)init {
    if ((self = [super init])) {
        self.paneName = @"Profile";
        self.paneSymbol = @"person.crop.circle";
    }
    return self;
}

- (void)loadView {
    _name = entryField(@"The name printed on every job sheet");
    _tagline = entryField(@"Printing & Design Services");
    _phone = entryField(@"+233 …");
    _address = entryField(@"Where the shop is");
    _currency = valueText(@"—");
    _momo = valueText(@"—");
    _install = valueText(@"—");

    self.status = statusText();
    NSGridView *grid = formGrid(@[@[captionText(@"Shop name:"), _name],
                                   @[captionText(@"Tagline:"), _tagline],
                                   @[captionText(@"Phone:"), _phone],
                                   @[captionText(@"Address:"), _address],
                                   @[captionText(@"Currency:"), _currency],
                                   @[captionText(@"MoMo number:"), _momo],
                                   @[captionText(@"Devices install from:"), _install],
                                   @[captionText(@"") , rowOf(@[pushButton(@"Save", self, @selector(save)),
                                                               pushButton(@"Load again", self, @selector(load))])]],
                                 @[noteText(@"These are the words that go on a job sheet and out to a client."),
                                   noteText(@"Kept in the book, so every device and every printed sheet sees "
                                            "the same shop. Only this Mac can change them."),
                                   self.status]);
    [self place:grid];
}

- (void)viewDidAppear {
    [super viewDidAppear];
    if (!_name.stringValue.length) [self load];
}

- (void)load {
    [self say:@"Reading the shop's words from the book…" colour:nil];
    __weak typeof(self) weak = self;
    callEngine(@"GET", @"api/shop", nil, ^(NSDictionary *json, NSString *why) {
        SettingsProfile *pane = weak;
        if (!pane) return;
        if (why) { [pane say:why colour:[NSColor systemRedColor]]; return; }
        NSDictionary *shop = json[@"shop"] ?: @{};
        pane->_name.stringValue = shop[@"name"] ?: @"";
        pane->_tagline.stringValue = shop[@"tagline"] ?: @"";
        pane->_phone.stringValue = shop[@"phone"] ?: @"";
        pane->_address.stringValue = shop[@"address"] ?: @"";
        pane->_currency.stringValue = [NSString stringWithFormat:@"%@ %@",
                                       shop[@"currency_symbol"] ?: @"", shop[@"currency"] ?: @""];
        pane->_momo.stringValue = json[@"momo_number"] ?: @"not set";
        pane->_install.stringValue = json[@"address"] ?: @"—";
        [pane say:@"Loaded from the book." colour:nil];
    });
}

- (void)save {
    NSDictionary *body = @{@"name": _name.stringValue ?: @"",
                           @"tagline": _tagline.stringValue ?: @"",
                           @"phone": _phone.stringValue ?: @"",
                           @"address": _address.stringValue ?: @""};
    [self say:@"Saving…" colour:nil];
    __weak typeof(self) weak = self;
    callEngine(@"PUT", @"api/shop", body, ^(NSDictionary *json, NSString *why) {
        SettingsProfile *pane = weak;
        if (!pane) return;
        if (why) { [pane say:why colour:[NSColor systemRedColor]]; return; }
        [pane say:@"Saved. New sheets and client messages carry these words from now on."
            colour:[NSColor systemGreenColor]];
    });
}
@end

// -- Updates: which build this is, and the one place anything is ever asked of the internet
@interface SettingsUpdates : SettingsPane
@end

@implementation SettingsUpdates {
    NSTextField *_newest;
}

- (instancetype)init {
    if ((self = [super init])) {
        self.paneName = @"Updates";
        self.paneSymbol = @"arrow.triangle.2.circlepath";
    }
    return self;
}

- (void)loadView {
    NSDictionary *info = [NSBundle mainBundle].infoDictionary;
    NSString *version = info[@"CFBundleShortVersionString"] ?: @"—";
    NSString *commit = info[@"CFBundleVersion"] ?: @"unknown";
    NSString *built = info[@"ChrisphicsBuildDate"] ?: @"unknown";

    _newest = statusText();
    [_newest setStringValue:[NSString stringWithFormat:
                             @"This build is %@, made %@.", version, built]];
    self.status = statusText();

    NSGridView *grid = formGrid(@[@[captionText(@"This build:"), valueText(version)],
                                   @[captionText(@"Commit:"), valueText(commit)],
                                   @[captionText(@"Made:"), valueText(built)],
                                   @[captionText(@"Repository:"), valueText(kRepository)],
                                   @[captionText(@"") , rowOf(@[pushButton(@"Check for update", self,
                                                                           @selector(check)),
                                                               pushButton(@"Open the repository", self,
                                                                          @selector(openRepo))])]],
                                 @[_newest,
                                   noteText(@"Nothing leaves this Mac unless Check is pressed: it asks GitHub "
                                            "for the newest version number in the shop's own repository and "
                                            "compares it with the build above. Each change to the app moves "
                                            "the number on — 1.0, 1.1, 1.2."),
                                   self.status]);
    [self place:grid];
}

- (void)openRepo {
    [[NSWorkspace sharedWorkspace] openURL:[NSURL URLWithString:kRepository]];
}

// 1.2 against 1.10: every dotted part is a number, so ten is ten and never a one.
static NSComparisonResult compareVersions(NSString *a, NSString *b) {
    NSArray<NSString *> *pa = [a componentsSeparatedByString:@"."];
    NSArray<NSString *> *pb = [b componentsSeparatedByString:@"."];
    NSInteger steps = MAX(pa.count, pb.count);
    for (NSInteger i = 0; i < steps; i++) {
        NSInteger x = i < pa.count ? pa[i].integerValue : 0;
        NSInteger y = i < pb.count ? pb[i].integerValue : 0;
        if (x < y) return NSOrderedAscending;
        if (x > y) return NSOrderedDescending;
    }
    return NSOrderedSame;
}

- (void)check {
    [_newest setStringValue:@"Asking GitHub…"];
    NSURL *url = [NSURL URLWithString:
                  @"https://raw.githubusercontent.com/HowellDaniel/ChrisphicsHub/main/VERSION"];
    NSMutableURLRequest *request = [NSMutableURLRequest requestWithURL:url];
    request.timeoutInterval = 10;
    NSString *mine = [NSBundle mainBundle].infoDictionary[@"CFBundleShortVersionString"] ?: @"0";
    [request setValue:[NSString stringWithFormat:@"%@/%@", kName, mine] forHTTPHeaderField:@"User-Agent"];
    NSURLSession *session = [NSURLSession sessionWithConfiguration:
                             [NSURLSessionConfiguration ephemeralSessionConfiguration]];
    __weak typeof(self) weak = self;
    [[session dataTaskWithRequest:request
               completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
                   NSInteger status = [(NSHTTPURLResponse *)response statusCode];
                   NSString *remote = data.length
                       ? [[[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding]
                          stringByTrimmingCharactersInSet:
                          [NSCharacterSet whitespaceAndNewlineCharacterSet]] : @"";
                   dispatch_async(dispatch_get_main_queue(), ^{
                       SettingsUpdates *pane = weak;
                       if (!pane) return;
                       if (error || status != 200 || !remote.length) {
                           [pane->_newest setStringValue:
                            [NSString stringWithFormat:@"GitHub could not be reached just now%@.",
                             error ? [NSString stringWithFormat:@" (%@)",
                                      error.localizedDescription] : @""]];
                           return;
                       }
                       NSString *local = [NSBundle mainBundle].infoDictionary[
                           @"CFBundleShortVersionString"] ?: @"0";
                       switch (compareVersions(local, remote)) {
                           case NSOrderedAscending:
                               [pane->_newest setStringValue:
                                [NSString stringWithFormat:@"Version %@ is on the repository and this "
                                 @"build is %@. Install a newer build of this app to take it in.",
                                 remote, local]];
                               break;
                           case NSOrderedSame:
                               [pane->_newest setStringValue:
                                [NSString stringWithFormat:@"This build (%@) is the newest version the "
                                 @"repository has.", local]];
                               break;
                           case NSOrderedDescending:
                               [pane->_newest setStringValue:
                                [NSString stringWithFormat:@"This build carries version %@ — the "
                                 @"repository is still on %@.", local, remote]];
                               break;
                       }
                   });
                   [session finishTasksAndInvalidate];
               }] resume];
}
@end

// ------------------------------------------------------------- the window they sit in
@implementation ShopSettings {
    NSWindow *_window;
    NSTabViewController *_tabs;
}

+ (ShopSettings *)shared {
    static ShopSettings *one = nil;
    static dispatch_once_t once;
    dispatch_once(&once, ^{ one = [[ShopSettings alloc] init]; });
    return one;
}

+ (void)show {
    [[self shared] open];
}

- (void)build {
    if (_window) return;
    _tabs = [[NSTabViewController alloc] init];
    _tabs.tabStyle = NSTabViewControllerTabStyleToolbar;
    NSArray *panes = @[[[SettingsGeneral alloc] init], [[SettingsAppearance alloc] init],
                       [[SettingsProfile alloc] init], [[SettingsUpdates alloc] init]];
    for (SettingsPane *pane in panes) {
        NSTabViewItem *item = [[NSTabViewItem alloc] initWithIdentifier:pane.paneName];
        item.label = pane.paneName;
        item.image = [NSImage imageWithSystemSymbolName:pane.paneSymbol
                               accessibilityDescription:pane.paneName];
        item.viewController = pane;
        [_tabs addTabViewItem:item];
    }
    _tabs.preferredContentSize = NSMakeSize(620, 460);

    _window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 620, 460)
                                          styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                                                     NSWindowStyleMaskMiniaturizable)
                                            backing:NSBackingStoreBuffered
                                              defer:NO];
    _window.title = [kName stringByAppendingString:@" Settings"];
    _window.releasedWhenClosed = NO;
    _window.tabbingMode = NSWindowTabbingModeDisallowed;
    _window.contentViewController = _tabs;
    [_window center];
}

- (void)open {
    [self build];
    [_window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
}

// ------------------------------------------------------------- describing itself
// A settings window is the one part of a Mac app no screen grab can prove from outside: it opens
// from a menu, and a check driving the menu needs the permissions the check itself must not ask
// for. So, as with --window-probe, the app is allowed to say what its window holds — and to be
// held to it: a pane that did not draw comes back one colour and no light.
+ (void)probeInto:(NSString *)folder done:(void (^)(void))done {
    // The menu item is what a person actually uses, so the probe takes that road and not a method
    // call: it finds the item where the menu bar holds it and sends the item's own action.
    NSMenuItem *item = nil;
    for (NSMenuItem *one in NSApp.mainMenu.itemArray.firstObject.submenu.itemArray)
        if ([one.title isEqualToString:@"Settings…"]) item = one;
    BOOL sent = item != nil && item.target != nil
        && [NSApp sendAction:item.action to:item.target from:item];
    ShopSettings *one = [self shared];
    if (!one->_window) [one open];
    NSString *menu = [NSString stringWithFormat:
                      @"menu: %@ %@, %@, %@",
                      item ? item.title : @"no Settings item",
                      item && item.isEnabled ? @"enabled" : @"disabled",
                      item.keyEquivalent.length ? [@"⌘" stringByAppendingString:item.keyEquivalent]
                                                : @"no key",
                      sent && one->_window.isVisible ? @"opened the window"
                                                     : @"did not open the window"];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.9 * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{ [one describePanes:folder at:0 lines:
                                                     [NSMutableArray arrayWithObject:menu] done:done]; });
}

- (void)describePanes:(NSString *)folder at:(NSInteger)index lines:(NSMutableArray *)lines
                 done:(void (^)(void))done {
    if (index >= (NSInteger)_tabs.tabViewItems.count) {
        NSString *text = [[lines componentsJoinedByString:@"\n"] stringByAppendingString:@"\n"];
        [[NSFileManager defaultManager] createDirectoryAtPath:folder withIntermediateDirectories:YES
                                                   attributes:nil error:nil];
        [text writeToFile:[folder stringByAppendingPathComponent:@"settings.txt"]
               atomically:YES encoding:NSUTF8StringEncoding error:nil];
        printf("%s", [text UTF8String]);
        fflush(stdout);
        [_window close];
        done ? done() : (void)0;
        return;
    }
    _tabs.selectedTabViewItemIndex = index;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.7 * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{
                       [self snapshotPane:folder at:index lines:lines done:done checking:YES];
                   });
}

// The update check is the one thing this app ever asks of the internet, so the probe presses the
// button a person would press, waits for GitHub's answer, and only then describes the pane.
- (void)snapshotPane:(NSString *)folder at:(NSInteger)index lines:(NSMutableArray *)lines
                done:(void (^)(void))done checking:(BOOL)allowCheck {
    NSTabViewItem *item = self->_tabs.tabViewItems[index];
    NSView *pane = item.viewController.view;
    [pane layoutSubtreeIfNeeded];
    NSButton *check = allowCheck ? [self buttonTitled:@"Check for update" in:pane] : nil;
    if (check) {
        [NSApp sendAction:check.action to:check.target from:check];
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(8.0 * NSEC_PER_SEC)),
                       dispatch_get_main_queue(), ^{
                           [self snapshotPane:folder at:index lines:lines done:done checking:NO];
                       });
        return;
    }
    NSMutableString *words = [NSMutableString stringWithFormat:@"%@:", item.label];
    // The pane's toolbar icon is part of what the window is meant to show, so the probe says
    // whether this tab item actually carries one rather than leaving a person to squint at a
    // screen grab of the pane alone.
    [words appendFormat:@" icon=%@", item.image ? @"set" : @"missing"];
    for (NSTextField *field in [self fieldsIn:pane]) {
        if (!field.stringValue.length) continue;
        [words appendFormat:@" %@=%@", field.placeholderString ?: @"value",
         field.stringValue];
    }
    NSBitmapImageRep *shot = nil;
    NSString *picture = [self capture:pane into:&shot];
    if (shot) {
        [[shot representationUsingType:NSBitmapImageFileTypePNG properties:@{}]
         writeToFile:[folder stringByAppendingPathComponent:
                    [NSString stringWithFormat:@"settings-%@.png",
                     [item.label lowercaseString]]] atomically:YES];
    }
    [lines addObject:[words stringByAppendingString:
                        [@" picture=" stringByAppendingString:picture]]];
    [self describePanes:folder at:index + 1 lines:lines done:done];
}

- (NSButton *)buttonTitled:(NSString *)title in:(NSView *)root {
    for (NSView *view in root.subviews) {
        if ([view isKindOfClass:[NSButton class]] && [((NSButton *)view).title isEqualToString:title])
            return (NSButton *)view;
        NSButton *inner = [self buttonTitled:title in:view];
        if (inner) return inner;
    }
    return nil;
}

- (NSArray<NSTextField *> *)fieldsIn:(NSView *)root {
    NSMutableArray *found = [NSMutableArray array];
    for (NSView *view in root.subviews) {
        if ([view isKindOfClass:[NSTextField class]] && ((NSTextField *)view).stringValue.length)
            [found addObject:(NSTextField *)view];
        if (view.subviews.count) [found addObjectsFromArray:[self fieldsIn:view]];
    }
    return found;
}

// The pane a person is looking at is inside the window, and the window is the thing the window
// server has actually drawn: a tab's own view can be offscreen for the first moment after it is
// selected, and caching that gives a correct-sized, entirely black picture — which is how the
// Updates pane came back from the first probe. So the window's content is drawn on demand and
// photographed instead, and every pane is described by what the window showed of it.
- (NSString *)capture:(NSView *)view into:(NSBitmapImageRep **)out {
    NSView *target = view.window.contentView ?: view;
    [target layoutSubtreeIfNeeded];
    [target.window displayIfNeeded];
    [target displayIfNeeded];
    NSBitmapImageRep *cached = [target bitmapImageRepForCachingDisplayInRect:target.bounds];
    if (!cached) return @"no picture";
    [target cacheDisplayInRect:target.bounds toBitmapImageRep:cached];
    NSData *png = [cached representationUsingType:NSBitmapImageFileTypePNG properties:@{}];
    NSImage *image = png ? [[NSImage alloc] initWithData:png] : nil;
    if (!image) return @"a picture that could not be read";

    // The cached rep is whatever shape AppKit chose for this display, and reading its bytes directly
    // gave a white pane that counted as one black colour. Redrawing the picture into a bitmap whose
    // layout this code owns — the same way the window probe reads the shop's own window — makes the
    // count mean what it says.
    NSInteger wide = cached.pixelsWide, high = cached.pixelsHigh;
    if (wide < 16 || high < 16)
        return [NSString stringWithFormat:@"a picture %ld x %ld wide", (long)wide, (long)high];
    NSBitmapImageRep *rep = [[NSBitmapImageRep alloc]
        initWithBitmapDataPlanes:NULL pixelsWide:wide pixelsHigh:high
                    bitsPerSample:8 samplesPerPixel:4 hasAlpha:YES isPlanar:NO
                   colorSpaceName:NSDeviceRGBColorSpace bytesPerRow:0 bitsPerPixel:0];
    if (!rep) return @"a picture that could not be read";
    [NSGraphicsContext saveGraphicsState];
    [NSGraphicsContext setCurrentContext:[NSGraphicsContext graphicsContextWithBitmapImageRep:rep]];
    // A settings pane paints only its controls: everywhere else the window's own background shows
    // through, and a cached picture of it is transparent there. Drawn onto nothing, every one of
    // those pixels counts as black — so the background is laid down first, as the window shows it.
    [[NSColor windowBackgroundColor] setFill];
    NSRectFill(NSMakeRect(0, 0, wide, high));
    [image drawInRect:NSMakeRect(0, 0, wide, high)];
    [NSGraphicsContext restoreGraphicsState];
    unsigned char *bytes = rep.bitmapData;
    NSInteger stride = rep.bytesPerRow, depth = rep.samplesPerPixel;
    NSMutableSet *colours = [NSMutableSet set];
    NSInteger least = 255, most = 0;
    for (NSInteger y = 0; y < rep.pixelsHigh; y += 3) {
        for (NSInteger x = 0; x < rep.pixelsWide; x += 3) {
            unsigned char *p = bytes + y * stride + x * depth;
            NSInteger red = p[0], green = p[1], blue = p[2];
            [colours addObject:@(((red >> 3) << 10) | ((green >> 3) << 5) | (blue >> 3))];
            NSInteger light = (red * 30 + green * 59 + blue * 11) / 100;
            if (light < least) least = light;
            if (light > most) most = light;
        }
    }
    *out = rep;
    return [NSString stringWithFormat:@"%ld x %ld, %lu colours, light %ld to %ld",
            (long)rep.pixelsWide, (long)rep.pixelsHigh, (unsigned long)colours.count,
            (long)least, (long)most];
}
@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        for (int i = 1; i < argc; i++) {
            if (!strcmp(argv[i], "--port") && i + 1 < argc) kPort = atol(argv[i + 1]);
            if (!strcmp(argv[i], "--window-probe") && i + 1 < argc)
                kProbeTo = [NSString stringWithUTF8String:argv[i + 1]];
            if (!strcmp(argv[i], "--settings-probe") && i + 1 < argc)
                kSettingsProbeTo = [NSString stringWithUTF8String:argv[i + 1]];
        }
        // Made here rather than at the first write, because the picture of the window is written
        // before the text that describes it, and a write into a folder that is not there fails quietly.
        if (kProbeTo) [[NSFileManager defaultManager] createDirectoryAtPath:kProbeTo
                                               withIntermediateDirectories:YES attributes:nil error:nil];
        if (kSettingsProbeTo) [[NSFileManager defaultManager] createDirectoryAtPath:kSettingsProbeTo
                                                          withIntermediateDirectories:YES
                                                                           attributes:nil error:nil];
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
