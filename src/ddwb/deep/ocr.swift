// OCR of PDF pages with macOS's own frameworks (PDFKit renders, Vision reads): nothing downloaded or installed.
// swift ocr.swift <pdf> <pages, counted from 1, comma-separated>  ->  JSON {"<page>": "<text, one line per line>"}
import AppKit
import Foundation
import PDFKit
import Vision

let args = CommandLine.arguments
guard args.count >= 3, let doc = PDFDocument(url: URL(fileURLWithPath: args[1])) else {
    FileHandle.standardError.write("usage: ocr.swift <pdf> <pages>\n".data(using: .utf8)!)
    exit(1)
}
var out: [String: String] = [:]
for p in args[2].split(separator: ",").compactMap({ Int($0) }) {
    guard let page = doc.page(at: p - 1) else { continue }
    let box = page.bounds(for: .mediaBox)
    let rotated = page.rotation % 180 != 0
    let scale: CGFloat = 200.0 / 72.0  // 200 dpi
    let w = Int((rotated ? box.height : box.width) * scale), h = Int((rotated ? box.width : box.height) * scale)
    guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                              space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { continue }
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    ctx.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: ctx)
    guard let image = ctx.makeImage() else { continue }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    try? VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    let lines = (request.results ?? []).sorted { a, b in  // top to bottom, then left to right within a row
        abs(a.boundingBox.midY - b.boundingBox.midY) > 0.008 ? a.boundingBox.midY > b.boundingBox.midY : a.boundingBox.minX < b.boundingBox.minX
    }
    var text = "", lastY: CGFloat = -1
    for o in lines {
        guard let s = o.topCandidates(1).first?.string else { continue }
        if lastY >= 0 && abs(o.boundingBox.midY - lastY) <= 0.008 { text += " | " + s } else { text += (text.isEmpty ? "" : "\n") + s }
        lastY = o.boundingBox.midY
    }
    out[String(p)] = text
}
FileHandle.standardOutput.write(try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]))
