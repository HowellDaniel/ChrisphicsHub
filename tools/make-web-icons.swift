// Renders the installable-app icons for phones and Windows, straight into public/img.
// The mark is the same four diamonds the Dock icon uses, drawn rather than pasted from
// the logo file so it stays crisp at 192px. Two plates are needed because the platforms
// mask differently: Chrome/Windows keep the artwork's own rounded badge, while
// `maskable` icons and Apple's launcher crop the image into a shape of their own, so
// those must be full-bleed with the mark inside the safe zone.
// Usage: swift tools/make-web-icons.swift [output-dir]   (default: public/img)

import Foundation
import CoreGraphics
import ImageIO

let outDir = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : FileManager.default.currentDirectoryPath + "/public/img"

func color(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> CGColor {
    CGColor(red: r, green: g, blue: b, alpha: a)
}

let crimson = color(0.788, 0.227, 0.247)   // the logo's red, lifted for a dark plate
let steel = color(0.604, 0.604, 0.627)     // the logo's grey, lifted likewise
let inkDark = color(0.106, 0.082, 0.090)
let inkLift = color(0.169, 0.125, 0.137)

func diamond(_ ctx: CGContext, _ cx: CGFloat, _ cy: CGFloat, _ h: CGFloat) {
    ctx.move(to: CGPoint(x: cx, y: cy + h))
    ctx.addLine(to: CGPoint(x: cx + h, y: cy))
    ctx.addLine(to: CGPoint(x: cx, y: cy - h))
    ctx.addLine(to: CGPoint(x: cx - h, y: cy))
    ctx.closePath()
}

func ink(_ ctx: CGContext, _ s: CGFloat) {
    let gradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                              colors: [inkDark, inkLift] as CFArray, locations: [0, 1])!
    ctx.drawLinearGradient(gradient, start: .zero, end: CGPoint(x: 0, y: s), options: [])
}

func mark(_ ctx: CGContext, _ s: CGFloat, _ scale: CGFloat) {
    let mid = s * 0.5
    let h = s * 0.154 * scale
    let off = h * 1.27
    ctx.setFillColor(steel)
    diamond(ctx, mid, mid + off, h)
    diamond(ctx, mid, mid - off, h)
    ctx.fillPath()
    ctx.setFillColor(crimson)
    diamond(ctx, mid - off, mid, h)
    diamond(ctx, mid + off, mid, h)
    ctx.fillPath()
}

/// `badge` keeps the plate's own rounded corners and transparent margin; the other two
/// fill the canvas edge to edge, with the mark shrunk for maskable's 80% safe zone.
func render(_ px: Int, _ plate: String) -> CGImage {
    let ctx = CGContext(data: nil, width: px, height: px, bitsPerComponent: 8, bytesPerRow: 0,
                        space: CGColorSpaceCreateDeviceRGB(),
                        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    let s = CGFloat(px)
    if plate == "badge" {
        let badge = CGRect(x: 0, y: 0, width: s, height: s).insetBy(dx: s * 0.085, dy: s * 0.085)
        let r = badge.width * 0.235
        ctx.saveGState()
        ctx.addPath(CGPath(roundedRect: badge, cornerWidth: r, cornerHeight: r, transform: nil))
        ctx.clip()
        ink(ctx, s)
        ctx.restoreGState()
        mark(ctx, s, 1)
        ctx.addPath(CGPath(roundedRect: badge, cornerWidth: r, cornerHeight: r, transform: nil))
        ctx.setStrokeColor(color(1, 1, 1, 0.14))
        ctx.setLineWidth(max(1, s * 0.006))
        ctx.strokePath()
    } else {
        ink(ctx, s)
        mark(ctx, s, plate == "maskable" ? 0.72 : 0.86)
    }
    return ctx.makeImage()!
}

func png(_ image: CGImage, _ name: String) {
    let url = URL(fileURLWithPath: outDir + "/" + name)
    guard let dest = CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil) else { return }
    CGImageDestinationAddImage(dest, image, nil)
    CGImageDestinationFinalize(dest)
    print(name)
}

try? FileManager.default.createDirectory(at: URL(fileURLWithPath: outDir), withIntermediateDirectories: true)
png(render(512, "badge"), "icon-512.png")
png(render(192, "badge"), "icon-192.png")
png(render(512, "maskable"), "icon-maskable-512.png")
png(render(180, "apple"), "apple-touch-icon.png")
