// Draws the QR that sends a phone to the shop address, as a plain PNG that can be printed,
// mailed or left on the counter. Every code is read back before the shop trusts it, because
// a QR that does not decode is worse than no QR.
// Usage: swift tools/make-qr.swift <text> <output.png> [pixels]
//        swift tools/make-qr.swift --verify <output.png>

import Foundation
import CoreImage
import ImageIO

let quiet = 4   // the blank margin every reader expects around a code
let argv = CommandLine.arguments
let ci = CIContext(options: [.useSoftwareRenderer: true])

func die(_ text: String) -> Never {
    FileHandle.standardError.write(Data((text + "\n").utf8))
    exit(1)
}

func read(_ url: URL) -> String? {
    guard let file = CIImage(contentsOf: url) else { return nil }
    let finder = CIDetector(ofType: "CIDetectorTypeQRCode", context: ci,
                            options: ["CIDetectorAccuracy": "CIDetectorAccuracyHigh",
                                      "CIDetectorMinTileSize": 4])
    // Codes this size get skipped on a first pass, so the same file is asked twice, once doubled.
    for plate in [file, file.transformed(by: CGAffineTransform(scaleX: 2, y: 2))] {
        if let hits = finder?.features(in: plate) as? [CIQRCodeFeature], let text = hits.first?.messageString {
            return text
        }
    }
    return nil
}

func draw(_ text: String, _ pixels: Int) -> CGImage {
    let filter = CIFilter(name: "CIQRCodeGenerator")!
    filter.setValue(Data(text.utf8), forKey: "inputMessage")
    filter.setValue("M", forKey: "inputCorrectionLevel")   // survives a smudged counter print
    guard let code = filter.outputImage else { die("That text would not fit a QR — keep the address short.") }
    let modules = Int(code.extent.width)
    let cell = (pixels + modules + quiet * 2 - 1) / (modules + quiet * 2)
    let side = cell * (modules + quiet * 2)
    let ctx = CGContext(data: nil, width: side, height: side, bitsPerComponent: 8, bytesPerRow: 0,
                        space: CGColorSpaceCreateDeviceRGB(),
                        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    ctx.interpolationQuality = .none   // whole-pixel cells keep each module a solid square
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: side, height: side))
    guard let plate = ci.createCGImage(code, from: code.extent) else { die("The QR could not be drawn.") }
    ctx.draw(plate, in: CGRect(x: quiet * cell, y: quiet * cell, width: modules * cell, height: modules * cell))
    return ctx.makeImage()!
}

func png(_ image: CGImage, _ url: URL) {
    guard let dest = CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil) else {
        die("Could not open \(url.path) for writing.")
    }
    CGImageDestinationAddImage(dest, image, nil)
    guard CGImageDestinationFinalize(dest) else { die("Could not write \(url.path) — check the folder and try again.") }
}

if argv.count >= 2, argv[1] == "--verify" {
    guard argv.count >= 3 else { die("Usage: swift tools/make-qr.swift --verify <file.png>") }
    let url = URL(fileURLWithPath: argv[2])
    guard let text = read(url) else { die("QR check failed: \(url.path) does not decode — draw it again.") }
    print("QR ok: \(text)")
    exit(0)
}

guard argv.count >= 3 else {
    die("Usage: swift tools/make-qr.swift <text> <output.png> [pixels]\n       swift tools/make-qr.swift --verify <output.png>")
}
let text = argv[1]
let url = URL(fileURLWithPath: argv[2])
let wanted: Int? = argv.count > 3 ? Int(argv[3]) : 520
guard let pixels = wanted, !text.isEmpty, pixels >= 80, pixels <= 4000 else {
    die("Give the text to encode, a file to write, and optionally a size from 80 to 4000 pixels.")
}

try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
png(draw(text, pixels), url)
print("QR written: \(url.path)")
guard let back = read(url) else { die("QR check failed: \(url.path) was written but does not decode.") }
guard back == text else { die("QR check failed: \(url.path) reads back as \"\(back)\", not the address given.") }
print("QR ok: \(back)")
