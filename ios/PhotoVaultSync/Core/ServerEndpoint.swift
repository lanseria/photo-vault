//
//  ServerEndpoint.swift
//  PhotoVaultSync
//
//  地址解析与拼装。这一层的存在理由（都是实际会踩的坑）：
//    1. 用户会填 `bmc_desktop:7878`（没有 scheme）；不补 http:// 会被解析成
//       scheme=bmc_desktop、path=7878，URLSession 直接报 unsupported URL。
//    2. 单段主机名（bmc_desktop）在 iOS 上不一定能解析 —— macOS 能，iPhone 有时不能。
//       所以顺便给出 `<host>.local` 候选，连接测试时依次试。
//    3. 服务器可能挂在反代的子路径下（http://host:7878/vault/），拼接口不能丢这段前缀。
//

import Foundation

enum ServerEndpoint {
    /// App 的默认地址（家里那台 Windows 机器）
    static let defaultAddress = "http://bmc_desktop:7878/"

    /// 用户输入 → 规范化 base URL（scheme/host/端口/子路径，path 以 / 结尾）
    static func normalize(_ raw: String?) -> URL? {
        guard var text = raw?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty else {
            return nil
        }
        // "host:7878" 会被 URLComponents 当成 scheme + path，必须先补协议
        if !text.contains("://") {
            text = "http://" + text
        }
        guard var comps = URLComponents(string: text),
              let host = comps.host, !host.isEmpty else { return nil }
        comps.scheme = (comps.scheme ?? "http").lowercased()
        guard comps.scheme == "http" || comps.scheme == "https" else { return nil }
        comps.user = nil
        comps.password = nil
        var path = comps.path
        if path.isEmpty { path = "/" }
        if !path.hasPrefix("/") { path = "/" + path }
        if !path.hasSuffix("/") { path += "/" }
        comps.path = path
        comps.query = nil
        comps.fragment = nil
        return comps.url
    }

    /// 连接测试用的候选地址：用户填的原样 + `<host>.local`（单段主机名时）
    static func candidates(for raw: String?) -> [URL] {
        guard let base = normalize(raw) else { return [] }
        var out = [base]
        if let host = base.host, !host.contains("."), host.lowercased() != "localhost" {
            var comps = URLComponents(url: base, resolvingAgainstBaseURL: false)
            comps?.host = host + ".local"
            if let alt = comps?.url, alt != base {
                out.append(alt)
            }
        }
        return out
    }

    /// base + 接口路径 + query
    static func path(_ base: URL, _ relative: String, query: [URLQueryItem] = []) -> URL? {
        var comps = URLComponents(url: base, resolvingAgainstBaseURL: false)
        var basePath = comps?.path ?? "/"
        if !basePath.hasSuffix("/") { basePath += "/" }
        var rel = relative
        while rel.hasPrefix("/") { rel.removeFirst() }
        comps?.path = basePath + rel
        comps?.queryItems = query.isEmpty ? nil : query
        return comps?.url
    }

    /// 地址是否指向局域网（用来判断"连不上"时是不是本地网络权限的问题）
    static func isLocalNetwork(_ url: URL) -> Bool {
        guard let host = url.host?.lowercased() else { return false }
        if host == "localhost" { return true }
        if host.hasSuffix(".local") { return true }
        if !host.contains(".") { return true }                    // 单段主机名
        if host.hasPrefix("10.") || host.hasPrefix("192.168.") { return true }
        if host.hasPrefix("172.") {
            let parts = host.split(separator: ".")
            if parts.count >= 2, let second = Int(parts[1]), (16...31).contains(second) { return true }
        }
        if host.hasPrefix("127.") || host.hasPrefix("169.254.") { return true }
        return false
    }
}
