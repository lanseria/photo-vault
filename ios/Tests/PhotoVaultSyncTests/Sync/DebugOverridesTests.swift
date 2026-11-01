//
//  DebugOverridesTests.swift
//  PhotoVaultSyncTests
//
//  DEBUG 启动参数解析单测：合法值、非法值、缺值、组合。
//  重点是「解析失败必须报错」——参数写错不能静默降级成全量上传。
//

import XCTest
@testable import PhotoVaultSync

final class DebugOverridesTests: XCTestCase {

    func testEmptyArgumentsProduceEmptyOverrides() throws {
        let overrides = try DebugOverrides.parse(arguments: ["/path/to/App"])
        XCTAssertTrue(overrides.isEmpty)
        XCTAssertNil(overrides.fromDate)
        XCTAssertNil(overrides.baseURL)
        XCTAssertNil(overrides.expectPlannedCount)
    }

    func testParseFromDateEpoch() throws {
        let overrides = try DebugOverrides.parse(
            arguments: ["App", "-syncFromDateEpoch", "1793462400"])
        XCTAssertEqual(overrides.fromDate, Date(timeIntervalSince1970: 1_793_462_400))
    }

    func testParseBaseURLAndExpectedCount() throws {
        let overrides = try DebugOverrides.parse(arguments: [
            "App", "-syncBaseURL", "http://127.0.0.1:1/",
            "-syncFromDateEpoch", "1793462400",
            "-expectPlannedCount", "1",
        ])
        XCTAssertEqual(overrides.baseURL, "http://127.0.0.1:1/")
        XCTAssertEqual(overrides.expectPlannedCount, 1)
        XCTAssertEqual(overrides.fromDate, Date(timeIntervalSince1970: 1_793_462_400))
        XCTAssertFalse(overrides.isEmpty)
    }

    func testIsoStringIsRejected() {
        // 之前踩过的坑：fromDate 参数是 Unix 秒，不是 ISO 字符串。
        // 解析失败必须抛错（引擎会因此拒绝同步），不能静默当成没设。
        XCTAssertThrowsError(try DebugOverrides.parse(
            arguments: ["App", "-syncFromDateEpoch", "2026-11-01T00:00+08"])) { error in
            guard case DebugOverrides.ParseError.invalidFromDate = error else {
                return XCTFail("应该是 invalidFromDate，实际：\(error)")
            }
        }
    }

    func testMissingValueIsRejected() {
        XCTAssertThrowsError(try DebugOverrides.parse(arguments: ["App", "-syncFromDateEpoch"])) { error in
            guard case DebugOverrides.ParseError.missingValue = error else {
                return XCTFail("应该是 missingValue，实际：\(error)")
            }
        }
        XCTAssertThrowsError(try DebugOverrides.parse(arguments: ["App", "-expectPlannedCount"]))
    }

    func testInvalidCountIsRejected() {
        for raw in ["abc", "-1", "1.5"] {
            XCTAssertThrowsError(try DebugOverrides.parse(
                arguments: ["App", "-expectPlannedCount", raw]), "raw=\(raw)") { error in
                guard case DebugOverrides.ParseError.invalidCount = error else {
                    return XCTFail("应该是 invalidCount，实际：\(error)")
                }
            }
        }
    }

    func testInvalidBaseURLIsRejected() {
        XCTAssertThrowsError(try DebugOverrides.parse(
            arguments: ["App", "-syncBaseURL", "ftp://example.com"])) { error in
            guard case DebugOverrides.ParseError.invalidURL = error else {
                return XCTFail("应该是 invalidURL，实际：\(error)")
            }
        }
    }

    func testUnrelatedArgumentsAreIgnored() throws {
        let overrides = try DebugOverrides.parse(arguments: [
            "App", "-AppleLanguages", "(zh-Hans)", "-autostartSync",
            "-syncFromDateEpoch", "1793462400",
        ])
        XCTAssertEqual(overrides.fromDate, Date(timeIntervalSince1970: 1_793_462_400))
    }

    func testSummaryMentionsEffectiveValues() {
        let overrides = DebugOverrides(fromDate: Date(timeIntervalSince1970: 1_793_462_400),
                                       baseURL: "http://127.0.0.1:1/",
                                       expectPlannedCount: 1)
        let summary = overrides.summary
        XCTAssertTrue(summary.contains("baseURL=http://127.0.0.1:1/"))
        XCTAssertTrue(summary.contains("expectPlannedCount=1"))
        XCTAssertTrue(summary.contains("fromDate="))
    }
}
