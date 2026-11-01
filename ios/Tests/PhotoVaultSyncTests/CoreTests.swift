//
//  CoreTests.swift
//  PhotoVaultSyncTests
//
//  Core 层（不依赖 Photos / UI）的单元测试：地址规范化、响应解码、设置持久化。
//  跑法：xcodebuild test -scheme PhotoVaultSync -destination 'platform=iOS Simulator,...'
//

import XCTest
@testable import PhotoVaultSync

final class ServerEndpointTests: XCTestCase {
    func testNormalizeAddsSchemeAndTrailingSlash() {
        let url = ServerEndpoint.normalize("bmc_desktop:7878")
        XCTAssertEqual(url?.absoluteString, "http://bmc_desktop:7878/")
    }

    func testNormalizeKeepsExplicitPath() {
        let url = ServerEndpoint.normalize("http://bmc_desktop:7878/vault")
        XCTAssertEqual(url?.absoluteString, "http://bmc_desktop:7878/vault/")
    }

    func testNormalizeTrimsWhitespace() {
        XCTAssertEqual(ServerEndpoint.normalize("  http://192.168.31.115:7878  ")?.absoluteString,
                       "http://192.168.31.115:7878/")
    }

    func testNormalizeRejectsGarbage() {
        XCTAssertNil(ServerEndpoint.normalize(nil))
        XCTAssertNil(ServerEndpoint.normalize(""))
        XCTAssertNil(ServerEndpoint.normalize("   "))
        XCTAssertNil(ServerEndpoint.normalize("ftp://example.com"))
        XCTAssertNil(ServerEndpoint.normalize("http://"))
    }

    func testCandidatesAddLocalForSingleLabelHost() {
        let urls = ServerEndpoint.candidates(for: "bmc_desktop:7878")
        XCTAssertEqual(urls.map(\.absoluteString),
                       ["http://bmc_desktop:7878/", "http://bmc_desktop.local:7878/"])
    }

    func testCandidatesDoNotDuplicateForDottedHost() {
        let urls = ServerEndpoint.candidates(for: "http://192.168.31.115:7878/")
        XCTAssertEqual(urls.map(\.absoluteString), ["http://192.168.31.115:7878/"])
    }

    func testPathBuildsQueryAndEncodesChineseNames() throws {
        let base = try XCTUnwrap(ServerEndpoint.normalize("http://bmc_desktop:7878/"))
        let url = try XCTUnwrap(ServerEndpoint.path(base, "api/upload", query: [
            URLQueryItem(name: "name", value: "照片 01.HEIC"),
            URLQueryItem(name: "library", value: "gallery"),
            URLQueryItem(name: "batch", value: "ABC-123"),
        ]))
        let comps = try XCTUnwrap(URLComponents(url: url, resolvingAgainstBaseURL: false))
        XCTAssertEqual(comps.path, "/api/upload")
        XCTAssertEqual(comps.queryItems?.first(where: { $0.name == "name" })?.value, "照片 01.HEIC")
        XCTAssertTrue(url.absoluteString.hasPrefix("http://bmc_desktop:7878/api/upload?"))
    }

    func testPathPreservesBaseSubpath() throws {
        let base = try XCTUnwrap(ServerEndpoint.normalize("http://host:7878/vault"))
        let url = try XCTUnwrap(ServerEndpoint.path(base, "/api/stats"))
        XCTAssertEqual(url.absoluteString, "http://host:7878/vault/api/stats")
    }

    func testLocalNetworkDetection() {
        func local(_ text: String) -> Bool {
            ServerEndpoint.isLocalNetwork(URL(string: text)!)
        }
        XCTAssertTrue(local("http://bmc_desktop:7878"))
        XCTAssertTrue(local("http://bmc_desktop.local:7878"))
        XCTAssertTrue(local("http://192.168.31.115:7878"))
        XCTAssertTrue(local("http://10.0.0.2:7878"))
        XCTAssertTrue(local("http://172.16.3.4:7878"))
        XCTAssertTrue(local("http://localhost:7878"))
        XCTAssertFalse(local("http://example.com:7878"))
        XCTAssertFalse(local("http://172.40.3.4:7878"))
    }
}

final class DecodingTests: XCTestCase {
    func testStatsDecodingFromServerShape() throws {
        let json = """
        {"count":19919,"gallery":15955,"material":3964,"favorites":0,"trash":0,
         "persons":82,"first":"1970-01-01T08:00:00+08:00","last":"2026-10-30T21:48:31+08:00",
         "device_count":34,"unknown_device":8409,
         "bytes":2159266870148,"media_root":"/media","media_host_root":"D:\\\\photo\\\\media",
         "material_bytes":13560498983,"material_duration_ms":7380698,
         "cinema_titles":0,"cinema_episodes":0,
         "devices":[{"model":"iPhone 12 Pro Max","count":3887}]}
        """
        let stats = try JSONDecoder().decode(RemoteStats.self, from: Data(json.utf8))
        XCTAssertEqual(stats.count, 19919)
        XCTAssertEqual(stats.gallery, 15955)
        XCTAssertEqual(stats.deviceCount, 34)
        XCTAssertEqual(stats.unknownDevice, 8409)
        XCTAssertEqual(stats.bytes, 2_159_266_870_148)
    }

    func testAssetDecodingIgnoresExtraFieldsAndNulls() throws {
        let json = """
        {"total": 2, "items": [
          {"id":"5ecaf7e8-332d-4b4c-aeee-99623333ebe4","kind":"video","library":"gallery",
           "sha":"a1371138381313d5f4bf9d9ef390d3fd2a5e7dd25f49536e15e69213bce8d9cd",
           "favorite":false,"taken":"2026-10-30T21:48:31","tz_source":"utc",
           "model":"大疆 Action2","device_id":47,"device_unknown":false,"device_kind":"actioncam",
           "device_raw":"大疆 Action2","width":2688,"height":2016,"duration_ms":86720,
           "bytes":1137432116,"has_thumb":true,"origin":"DJI_0070.MP4",
           "raw_ext":"","raw_bytes":0,"tags":[],"deleted":false,"some_new_field":1},
          {"id":"x","kind":"photo","library":"gallery","sha":"","favorite":true,
           "taken":null,"model":"","width":null,"height":null,"duration_ms":null,
           "bytes":12,"has_thumb":false,"origin":"照片 01.HEIC","tags":["海边"]}
        ]}
        """
        let page = try JSONDecoder().decode(AssetPage.self, from: Data(json.utf8))
        XCTAssertEqual(page.total, 2)
        XCTAssertEqual(page.items.count, 2)
        XCTAssertEqual(page.items[0].fileName, "DJI_0070.MP4")
        XCTAssertEqual(page.items[0].durationText, "1:26")
        XCTAssertEqual(page.items[1].fileName, "照片 01.HEIC")
        XCTAssertEqual(page.items[1].tags, ["海边"])
        XCTAssertNil(page.items[1].takenDate)
        XCTAssertTrue(page.items[0].thumbnailAvailable)
        XCTAssertFalse(page.items[1].thumbnailAvailable)
    }

    func testUploadResponseDuplicate() throws {
        let json = #"{"status":"duplicate","asset":{"id":"a","kind":"live_photo","library":"gallery"},"queued":0,"bytes":123}"#
        let res = try JSONDecoder().decode(UploadResponse.self, from: Data(json.utf8))
        XCTAssertTrue(res.isDuplicate)
        XCTAssertTrue(res.isSuccess)
        XCTAssertEqual(res.asset?.kind, "live_photo")
        XCTAssertEqual(res.bytes, 123)
    }

    func testErrorDetailParsing() {
        XCTAssertEqual(
            APIClient.detail(from: Data(#"{"detail":"不支持的文件类型：.txt"}"#.utf8)),
            "不支持的文件类型：.txt"
        )
        XCTAssertEqual(APIClient.detail(from: Data()), "")
    }
}

@MainActor
final class SettingsStoreTests: XCTestCase {
    private var defaults: UserDefaults!

    override func setUp() {
        super.setUp()
        let name = "PhotoVaultSyncTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: name)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: defaults.description)
        defaults = nil
        super.tearDown()
    }

    func testDefaultAddressAndPersistence() {
        let store = SettingsStore(defaults: defaults)
        XCTAssertEqual(store.serverInput, ServerEndpoint.defaultAddress)
        XCTAssertEqual(store.library, "gallery")
        XCTAssertNil(store.effectiveBase)

        store.serverInput = "192.168.31.115:7878"
        store.library = "material"
        store.options.concurrentUploads = 3
        store.options.includeVideos = false

        let reloaded = SettingsStore(defaults: defaults)
        XCTAssertEqual(reloaded.serverInput, "192.168.31.115:7878")
        XCTAssertEqual(reloaded.library, "material")
        XCTAssertEqual(reloaded.options.concurrentUploads, 3)
        XCTAssertFalse(reloaded.options.includeVideos)
    }

    func testResolvedBaseFallsBackToNormalizedInput() {
        let store = SettingsStore(defaults: defaults)
        store.serverInput = "bmc_desktop:7878"
        XCTAssertEqual(store.resolvedBase?.absoluteString, "http://bmc_desktop:7878/")
        XCTAssertNoThrow(try store.makeClient())
    }

    func testNoteSyncFinishedPersists() {
        let store = SettingsStore(defaults: defaults)
        store.noteSyncFinished()
        let reloaded = SettingsStore(defaults: defaults)
        XCTAssertNotNil(reloaded.lastSyncAt)
    }

    func testMediaSummary() {
        var options = SyncOptions.default
        XCTAssertEqual(options.mediaSummary, "照片 · 视频 · 实况")
        options.includeLivePhotos = false
        XCTAssertEqual(options.mediaSummary, "照片 · 视频")
        options.includePhotos = false
        options.includeVideos = false
        XCTAssertEqual(options.mediaSummary, "未选择")
    }
}

final class FormatTests: XCTestCase {
    func testBytes() {
        XCTAssertEqual(Format.bytes(0), "0 B")
        XCTAssertEqual(Format.bytes(999), "999 B")
        XCTAssertEqual(Format.bytes(1024), "1.0 KB")
        XCTAssertEqual(Format.bytes(2_159_266_870_148), "2.0 TB")
    }

    func testDuration() {
        XCTAssertEqual(Format.duration(nil), "—")
        XCTAssertEqual(Format.duration(8), "8 秒")
        XCTAssertEqual(Format.duration(65), "1 分 5 秒")
        XCTAssertEqual(Format.duration(3725), "1 时 2 分")
    }

    func testSyncStateProgress() {
        var state = SyncState()
        XCTAssertNil(state.fraction)
        state.total = 20
        state.completed = 5
        XCTAssertEqual(state.fraction, 0.25)
        state.completed = 25
        XCTAssertEqual(state.fraction, 1.0)
    }
}

// MARK: - APIClient（用 MockURLProtocol 拦请求，不联网）

final class MockURLProtocol: URLProtocol {
    nonisolated(unsafe) static var handler: ((URLRequest) throws -> (HTTPURLResponse, Data))?
    nonisolated(unsafe) static var lastRequest: URLRequest?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.lastRequest = request
        guard let handler = Self.handler else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

final class APIClientTests: XCTestCase {
    private func makeClient(base: String = "http://bmc_desktop:7878/") -> APIClient {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.protocolClasses = [MockURLProtocol.self]
        return APIClient(baseURL: URL(string: base)!, timeout: 3, session: URLSession(configuration: cfg))
    }

    private func respond(_ body: String, status: Int = 200) {
        MockURLProtocol.handler = { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: status,
                                           httpVersion: "HTTP/1.1", headerFields: nil)!
            return (response, Data(body.utf8))
        }
    }

    override func tearDown() {
        MockURLProtocol.handler = nil
        MockURLProtocol.lastRequest = nil
        super.tearDown()
    }

    func testStatsRequestPathAndDecoding() async throws {
        respond(#"{"count":19919,"gallery":15955,"material":3964,"bytes":2159266870148}"#)
        let stats = try await makeClient().stats()
        XCTAssertEqual(stats.count, 19919)
        XCTAssertEqual(MockURLProtocol.lastRequest?.url?.path, "/api/stats")
    }

    func testAssetsQueryItems() async throws {
        respond(#"{"total":1,"items":[{"id":"a1","kind":"photo","library":"gallery","sha":"","favorite":false,"model":"","has_thumb":false,"origin":"IMG_0001.HEIC"}]}"#)
        let page = try await makeClient().assets(limit: 60, offset: 120, library: "material", kind: "video,live_photo")
        XCTAssertEqual(page.total, 1)
        XCTAssertEqual(page.items.first?.fileName, "IMG_0001.HEIC")
        let url = try XCTUnwrap(MockURLProtocol.lastRequest?.url)
        let comps = try XCTUnwrap(URLComponents(url: url, resolvingAgainstBaseURL: false))
        let items = Dictionary(uniqueKeysWithValues: (comps.queryItems ?? []).map { ($0.name, $0.value ?? "") })
        XCTAssertEqual(items["limit"], "60")
        XCTAssertEqual(items["offset"], "120")
        XCTAssertEqual(items["library"], "material")
        XCTAssertEqual(items["kind"], "video,live_photo")
    }

    func testHTTPErrorCarriesServerDetail() async {
        respond(#"{"detail":"不支持的文件类型：.txt"}"#, status: 415)
        do {
            _ = try await makeClient().stats()
            XCTFail("应该抛错")
        } catch let error as ServerError {
            XCTAssertEqual(error.errorDescription, "不支持的文件类型：.txt")
        } catch {
            XCTFail("错误类型不对：\(error)")
        }
    }

    func testTransportErrorIsClassifiedAsNetworkProblem() async {
        MockURLProtocol.handler = { _ in throw URLError(.cannotConnectToHost) }
        do {
            _ = try await makeClient().stats()
            XCTFail("应该抛错")
        } catch let error as ServerError {
            XCTAssertTrue(error.isNetworkProblem)
            XCTAssertTrue((error.errorDescription ?? "").contains("本地网络"))
        } catch {
            XCTFail("错误类型不对：\(error)")
        }
    }

    func testStaticURLBuilders() {
        let client = makeClient(base: "http://bmc_desktop:7878/")
        XCTAssertEqual(client.thumbURL(sha: String(repeating: "a", count: 64))?.absoluteString,
                       "http://bmc_desktop:7878/thumbs/\(String(repeating: "a", count: 64)).webp")
        XCTAssertNil(client.thumbURL(sha: "短"))
        XCTAssertEqual(client.previewURL(assetID: "abc")?.absoluteString,
                       "http://bmc_desktop:7878/preview/abc")
        XCTAssertEqual(client.fileURL(assetID: "abc", role: "live_motion")?.absoluteString,
                       "http://bmc_desktop:7878/file/abc?role=live_motion")
        XCTAssertEqual(client.fileURL(assetID: "abc")?.absoluteString,
                       "http://bmc_desktop:7878/file/abc")
    }
}

final class UploadTakenQueryTests: XCTestCase {
    /// taken 要编成 ISO8601 UTC（服务端契约），且能被同样格式还原回同一瞬时
    func testTakenQueryValueIsISO8601UTC() throws {
        let date = Date(timeIntervalSince1970: 1_725_154_200)   // 2024-09-01T01:30:00Z
        let text = UploadClient.takenQueryValue(date)
        XCTAssertEqual(text, "2024-09-01T01:30:00Z")

        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        formatter.timeZone = TimeZone(identifier: "UTC")
        let parsed = try XCTUnwrap(formatter.date(from: text))
        XCTAssertEqual(parsed, date)
    }
}
