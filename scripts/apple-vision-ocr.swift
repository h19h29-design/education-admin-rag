import CoreGraphics
import Foundation
import Vision

private let runtimeSchema = "sen-qa-apple-vision-runtime/v1"
private let linesSchema = "sen-qa-apple-vision-lines/v1"
private let requestRevision = 3
private let maximumDimension = 20_000
private let maximumNormalizedBoundingBoxOverflow = 0.005

private struct RuntimeInfo: Encodable {
    let architecture: String
    let engine: String
    let language: String
    let os_build: String
    let recognition_level: String
    let request_revision: Int
    let schema_version: String
    let uses_language_correction: Bool
}

private struct OutputLine: Encodable {
    let text: String
    let bbox: [Double]
    let confidence: Double
}

private struct LinesOutput: Encodable {
    let schema_version: String
    let lines: [OutputLine]
}

private struct GeometryOutput: Encodable {
    let bbox: [Double]
}

private enum FixedFailure: Error {
    case invalidArguments
    case invalidInput
    case unavailableRuntime
    case recognitionFailed
    case outputFailed
}

private func architectureName() -> String {
    #if arch(arm64)
    return "arm64"
    #elseif arch(x86_64)
    return "x86_64"
    #else
    return "unsupported"
    #endif
}

private func canonicalJSON<T: Encodable>(_ value: T) throws -> Data {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    var data = try encoder.encode(value)
    data.append(0x0A)
    return data
}

private func writeStandardOutput<T: Encodable>(_ value: T) throws {
    let data = try canonicalJSON(value)
    try FileHandle.standardOutput.write(contentsOf: data)
}

private func parsePositiveInteger(_ value: String) -> Int? {
    guard !value.isEmpty, value.count <= 6, value.allSatisfy(\.isNumber),
          let number = Int(value), number > 0, number <= maximumDimension else {
        return nil
    }
    return number
}

private func checkedByteCount(width: Int, height: Int) -> Int? {
    let (pixels, pixelOverflow) = width.multipliedReportingOverflow(by: height)
    let (bytes, byteOverflow) = pixels.multipliedReportingOverflow(by: 3)
    guard !pixelOverflow, !byteOverflow, bytes > 0 else { return nil }
    return bytes
}

private func readExactStandardInput(byteCount: Int) throws -> Data {
    var result = Data()
    result.reserveCapacity(byteCount)
    while result.count <= byteCount {
        let remaining = byteCount + 1 - result.count
        let chunk = try FileHandle.standardInput.read(upToCount: min(1_048_576, remaining))
        guard let chunk, !chunk.isEmpty else { break }
        result.append(chunk)
    }
    guard result.count == byteCount else { throw FixedFailure.invalidInput }
    return result
}

private func makeImage(width: Int, height: Int, rgbBytes: Data) throws -> CGImage {
    guard let provider = CGDataProvider(data: rgbBytes as CFData),
          let image = CGImage(
              width: width,
              height: height,
              bitsPerComponent: 8,
              bitsPerPixel: 24,
              bytesPerRow: width * 3,
              space: CGColorSpaceCreateDeviceRGB(),
              bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.none.rawValue),
              provider: provider,
              decode: nil,
              shouldInterpolate: false,
              intent: .defaultIntent
          ) else {
        throw FixedFailure.invalidInput
    }
    return image
}

private func configuredRequest() throws -> VNRecognizeTextRequest {
    let request = VNRecognizeTextRequest()
    request.revision = requestRevision
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["ko-KR"]
    request.usesLanguageCorrection = true
    let supported = try request.supportedRecognitionLanguages()
    guard supported.contains("ko-KR") else { throw FixedFailure.unavailableRuntime }
    return request
}

private func boundedPixelBoundingBox(
    minX: Double,
    minY: Double,
    maxX: Double,
    maxY: Double,
    width: Int,
    height: Int
) throws -> [Double] {
    let coordinates = [minX, minY, maxX, maxY]
    guard coordinates.allSatisfy(\.isFinite),
          minX >= -maximumNormalizedBoundingBoxOverflow,
          minY >= -maximumNormalizedBoundingBoxOverflow,
          maxX <= 1.0 + maximumNormalizedBoundingBoxOverflow,
          maxY <= 1.0 + maximumNormalizedBoundingBoxOverflow else {
        throw FixedFailure.recognitionFailed
    }
    let boundedMinX = min(1.0, max(0.0, minX))
    let boundedMinY = min(1.0, max(0.0, minY))
    let boundedMaxX = min(1.0, max(0.0, maxX))
    let boundedMaxY = min(1.0, max(0.0, maxY))
    guard boundedMinX < boundedMaxX, boundedMinY < boundedMaxY else {
        throw FixedFailure.recognitionFailed
    }
    return [
        boundedMinX * Double(width),
        (1.0 - boundedMaxY) * Double(height),
        boundedMaxX * Double(width),
        (1.0 - boundedMinY) * Double(height),
    ]
}

private func runtimeInfo() throws -> RuntimeInfo {
    _ = try configuredRequest()
    return RuntimeInfo(
        architecture: architectureName(),
        engine: "apple-vision",
        language: "ko-KR",
        os_build: ProcessInfo.processInfo.operatingSystemVersionString,
        recognition_level: "accurate",
        request_revision: requestRevision,
        schema_version: runtimeSchema,
        uses_language_correction: true
    )
}

private func recognize(width: Int, height: Int, rgbBytes: Data) throws -> LinesOutput {
    let image = try makeImage(width: width, height: height, rgbBytes: rgbBytes)
    let request = try configuredRequest()
    let handler = VNImageRequestHandler(cgImage: image, orientation: .up, options: [:])
    try handler.perform([request])
    let observations = request.results ?? []
    var output: [OutputLine] = []
    output.reserveCapacity(observations.count)
    for observation in observations {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox
        let pixelBox = try boundedPixelBoundingBox(
            minX: box.minX,
            minY: box.minY,
            maxX: box.maxX,
            maxY: box.maxY,
            width: width,
            height: height
        )
        output.append(
            OutputLine(
                text: candidate.string,
                bbox: pixelBox,
                confidence: Double(candidate.confidence)
            )
        )
    }
    output.sort {
        let left = ($0.bbox[1], $0.bbox[0], $0.bbox[3], $0.bbox[2], $0.text)
        let right = ($1.bbox[1], $1.bbox[0], $1.bbox[3], $1.bbox[2], $1.text)
        return left < right
    }
    return LinesOutput(schema_version: linesSchema, lines: output)
}

private func run() throws {
    let arguments = Array(CommandLine.arguments.dropFirst())
#if SEN_QA_GEOMETRY_TEST
    if arguments.count == 7,
       arguments[0] == "--geometry-test",
       let minX = Double(arguments[1]),
       let minY = Double(arguments[2]),
       let maxX = Double(arguments[3]),
       let maxY = Double(arguments[4]),
       let width = parsePositiveInteger(arguments[5]),
       let height = parsePositiveInteger(arguments[6]) {
        try writeStandardOutput(
            GeometryOutput(
                bbox: try boundedPixelBoundingBox(
                    minX: minX,
                    minY: minY,
                    maxX: maxX,
                    maxY: maxY,
                    width: width,
                    height: height
                )
            )
        )
        return
    }
#endif
    if arguments == ["--runtime-info"] {
        try writeStandardOutput(runtimeInfo())
        return
    }
    guard arguments.count == 6,
          arguments[0] == "--width",
          arguments[2] == "--height",
          arguments[4] == "--pixel-format",
          arguments[5] == "rgb8",
          let width = parsePositiveInteger(arguments[1]),
          let height = parsePositiveInteger(arguments[3]),
          let byteCount = checkedByteCount(width: width, height: height) else {
        throw FixedFailure.invalidArguments
    }
    let rgbBytes = try readExactStandardInput(byteCount: byteCount)
    try writeStandardOutput(recognize(width: width, height: height, rgbBytes: rgbBytes))
}

do {
    try run()
} catch {
    FileHandle.standardError.write(Data("apple-vision-ocr-failed\n".utf8))
    exit(1)
}
