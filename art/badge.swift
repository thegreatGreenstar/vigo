// Draw a two-letter badge on the icon, where the original mark sat.
//
//   swift art/badge.swift in.png out.png "VI" 196 99 0.345 0.459 0.855
//
// x and y are the centre in image pixels, counted from the top left.  The
// text is drawn in the same flat colour as the petals, at a size and angle
// that match the mark it replaces.

import AppKit
import CoreText

let arguments = CommandLine.arguments
guard arguments.count >= 8 else {
    FileHandle.standardError.write(
        "usage: badge in.png out.png TEXT X Y R G B [size] [angle]\n".data(using: .utf8)!)
    exit(2)
}
let inputPath = arguments[1]
let outputPath = arguments[2]
let text = arguments[3]
let centreX = Double(arguments[4]) ?? 0
let centreY = Double(arguments[5]) ?? 0
let red = Double(arguments[6]) ?? 0
let green = Double(arguments[7]) ?? 0
let blue = Double(arguments[8]) ?? 0
let size = arguments.count > 9 ? (Double(arguments[9]) ?? 19) : 19
let angle = arguments.count > 10 ? (Double(arguments[10]) ?? 0) : 0

guard let image = NSImage(contentsOfFile: inputPath),
      let tiff = image.tiffRepresentation,
      let source = NSBitmapImageRep(data: tiff) else {
    FileHandle.standardError.write("cannot read \(inputPath)\n".data(using: .utf8)!)
    exit(1)
}

let width = source.pixelsWide
let height = source.pixelsHigh
guard let target = NSBitmapImageRep(
    bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height,
    bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
    colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else {
    FileHandle.standardError.write("cannot make a canvas\n".data(using: .utf8)!)
    exit(1)
}

NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: target)
source.draw(in: NSRect(x: 0, y: 0, width: width, height: height))

if let context = NSGraphicsContext.current?.cgContext {
    context.setShouldAntialias(true)
    context.interpolationQuality = .high
    // The bitmap's origin is bottom left; the caller counts from the top.
    let x = centreX
    let y = Double(height) - centreY
    context.translateBy(x: x, y: y)
    context.rotate(by: angle * Double.pi / 180)

    let font = NSFont.systemFont(ofSize: size, weight: .bold)
    let attributes: [NSAttributedString.Key: Any] = [
        .font: font,
        .foregroundColor: NSColor(srgbRed: red, green: green, blue: blue, alpha: 1),
        .kern: -0.4,
    ]
    let line = CTLineCreateWithAttributedString(NSAttributedString(string: text, attributes: attributes))
    let bounds = CTLineGetBoundsWithOptions(line, .useGlyphPathBounds)
    context.textPosition = CGPoint(x: -bounds.width / 2, y: -bounds.height / 2 - bounds.origin.y)
    CTLineDraw(line, context)
}
NSGraphicsContext.restoreGraphicsState()

guard let data = target.representation(using: .png, properties: [:]) else { exit(1) }
try data.write(to: URL(fileURLWithPath: outputPath))
print("\(outputPath): \(width)x\(height) with \(text)")
