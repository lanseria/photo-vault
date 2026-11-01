//
//  RetryPolicyTests.swift
//  PhotoVaultSyncTests
//
//  重试策略单测：网络/服务端/永久/取消四类失败的分流与退避序列。
//

import XCTest
@testable import PhotoVaultSync

final class RetryPolicyTests: XCTestCase {

    func testNetworkFailureIsRetryable() {
        XCTAssertTrue(RetryPolicy.isRetryable(UploadError.network(message: "timeout")))
        XCTAssertEqual(RetryPolicy.kind(of: UploadError.network(message: "x")), .network)
    }

    func testServerBusyIsRetryableButClientErrorIsNot() {
        XCTAssertTrue(RetryPolicy.isRetryable(UploadError.server(status: 500, detail: "")))
        XCTAssertTrue(RetryPolicy.isRetryable(UploadError.server(status: 503, detail: "")))
        XCTAssertTrue(RetryPolicy.isRetryable(UploadError.server(status: 429, detail: "")))
        XCTAssertTrue(RetryPolicy.isRetryable(UploadError.server(status: 408, detail: "")))

        XCTAssertFalse(RetryPolicy.isRetryable(UploadError.server(status: 400, detail: "bad")))
        XCTAssertFalse(RetryPolicy.isRetryable(UploadError.server(status: 413, detail: "")))
        XCTAssertFalse(RetryPolicy.isRetryable(UploadError.server(status: 415, detail: "")))
        XCTAssertEqual(RetryPolicy.kind(of: UploadError.server(status: 400, detail: "bad")), .server)
    }

    func testCancelledIsNeverRetried() {
        XCTAssertFalse(RetryPolicy.isRetryable(UploadError.cancelled))
        XCTAssertEqual(RetryPolicy.kind(of: UploadError.cancelled), .cancelled)
        XCTAssertTrue(RetryPolicy.isCancellation(UploadError.cancelled))
        XCTAssertTrue(RetryPolicy.isCancellation(CancellationError()))
        XCTAssertTrue(RetryPolicy.isCancellation(URLError(.cancelled)))
        XCTAssertFalse(RetryPolicy.isCancellation(UploadError.network(message: "x")))
    }

    func testExportAndDecodeFailures() {
        XCTAssertTrue(RetryPolicy.isRetryable(PhotoLibraryError.exportFailed("iCloud 下载超时")))
        XCTAssertFalse(RetryPolicy.isRetryable(PhotoLibraryError.assetNotFound))
        XCTAssertFalse(RetryPolicy.isRetryable(PhotoLibraryError.resourceNotFound("x")))
        XCTAssertFalse(RetryPolicy.isRetryable(UploadError.invalidResponse("bad json")))
        XCTAssertEqual(RetryPolicy.kind(of: UploadError.invalidResponse("x")), .permanent)
    }

    func testBackoffSequence() {
        XCTAssertEqual(RetryPolicy.delaySeconds(afterAttempt: 1), 0.5)
        XCTAssertEqual(RetryPolicy.delaySeconds(afterAttempt: 2), 1.5)
        XCTAssertEqual(RetryPolicy.delaySeconds(afterAttempt: 3), 4)
        XCTAssertEqual(RetryPolicy.delaySeconds(afterAttempt: 9), 4)
        XCTAssertEqual(RetryPolicy.maxAttempts, 3)
    }
}
