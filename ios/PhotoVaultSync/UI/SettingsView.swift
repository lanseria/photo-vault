//
//  SettingsView.swift
//  PhotoVaultSync
//
//  设置页：服务器地址与连接测试、目标库、同步选项、相册权限、诊断日志、关于。
//

import SwiftUI
import UIKit

struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @Environment(SettingsStore.self) private var settings

    var body: some View {
        ScrollView {
            VStack(spacing: 14) {
                PageHeader(
                    eyebrow: "SETTINGS",
                    title: "设置",
                    subtitle: "服务器、同步范围、相册权限与诊断"
                )
                ServerSettingsCard()
                LibrarySettingsCard()
                SyncOptionsCard()
                PermissionSettingsCard()
                DiagnosticsCard()
                AboutCard()
            }
            .vaultPagePadding()
            .padding(.top, 2)
        }
        .scrollIndicators(.hidden)
        .background(VaultBackground())
    }
}

// MARK: - 服务器

private struct ServerSettingsCard: View {
    @Environment(AppModel.self) private var model
    @Environment(SettingsStore.self) private var settings
    @FocusState private var addressFocused: Bool

    var body: some View {
        @Bindable var settings = settings

        VaultCard {
            VStack(alignment: .leading, spacing: 12) {
                cardTitle("服务器", icon: "externaldrive.connected.to.line.below")

                HStack(spacing: 8) {
                    TextField("http://bmc_desktop:7878", text: $settings.serverInput)
                        .font(.system(.footnote, design: .monospaced))
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                        .submitLabel(.go)
                        .focused($addressFocused)
                        .onSubmit {
                            addressFocused = false
                            Task { await model.testConnection() }
                        }
                        .padding(.horizontal, 10)
                        .padding(.vertical, 9)
                        .background(
                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .fill(Color.black.opacity(0.28))
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .strokeBorder(Color.white.opacity(0.10), lineWidth: 1)
                        )

                    Button {
                        addressFocused = false
                        Task { await model.testConnection() }
                    } label: {
                        if settings.isProbing || model.health == .checking {
                            ProgressView().controlSize(.small).tint(Theme.accent)
                        } else {
                            Text("测试")
                        }
                    }
                    .buttonStyle(VaultMiniButtonStyle(tint: Theme.accent))
                    .disabled(settings.isProbing)
                    .accessibilityLabel("测试服务器连接")
                }

                connectionRow

                if let error = settings.lastProbeError, !model.health.isOnline {
                    Text(error.localizedDescription)
                        .font(.caption2)
                        .foregroundStyle(Theme.danger.opacity(0.9))
                        .fixedSize(horizontal: false, vertical: true)
                }

                HStack(alignment: .center) {
                    Text("默认地址 \(ServerEndpoint.defaultAddress)")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                    Spacer(minLength: 6)
                    Button("恢复默认") {
                        settings.serverInput = ServerEndpoint.defaultAddress
                        Task { await model.testConnection() }
                    }
                    .buttonStyle(VaultMiniButtonStyle())
                    .accessibilityLabel("恢复默认服务器地址")
                }
            }
        }
    }

    @ViewBuilder
    private var connectionRow: some View {
        HStack(spacing: 7) {
            switch model.health {
            case .online:
                StatusDot(color: Theme.success)
                Text("当前连通：\(settings.effectiveBase?.absoluteString ?? "—")")
                    .font(.system(.caption2, design: .monospaced, weight: .medium))
                    .foregroundStyle(Theme.success.opacity(0.95))
                    .lineLimit(1)
                    .truncationMode(.middle)
            case .checking:
                StatusDot(color: Theme.warning, pulsing: true)
                Text("正在连接…")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            case .offline:
                StatusDot(color: Theme.danger)
                Text("未连通，检查地址或服务器的本地网络权限")
                    .font(.caption2)
                    .foregroundStyle(Theme.danger.opacity(0.9))
                    .lineLimit(2)
            case .unknown:
                StatusDot(color: Color.gray)
                Text("还没测试过连接")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
    }

    private func cardTitle(_ title: String, icon: String) -> some View {
        HStack(spacing: 7) {
            Image(systemName: icon)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Theme.accent)
            Text(title)
                .font(.system(.subheadline, design: .rounded, weight: .semibold))
        }
    }
}

// MARK: - 目标库

private struct LibrarySettingsCard: View {
    @Environment(SettingsStore.self) private var settings

    var body: some View {
        @Bindable var settings = settings

        VaultCard {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 7) {
                    Image(systemName: "square.stack.3d.up.fill")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(Theme.accent)
                    Text("目标库")
                        .font(.system(.subheadline, design: .rounded, weight: .semibold))
                }

                Picker("目标库", selection: $settings.library) {
                    Text("相册").tag("gallery")
                    Text("素材库").tag("material")
                }
                .pickerStyle(.segmented)
                .accessibilityLabel("选择同步的目标库")

                Text("上传的照片会归到这个库。相册放日常照片，素材库放剪辑素材；图库页默认浏览同一目标库。")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

// MARK: - 同步选项

private struct SyncOptionsCard: View {
    @Environment(SettingsStore.self) private var settings

    var body: some View {
        VaultCard {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 7) {
                    Image(systemName: "slider.horizontal.3")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(Theme.accent)
                    Text("同步选项")
                        .font(.system(.subheadline, design: .rounded, weight: .semibold))
                }
                .padding(.bottom, 6)

                toggleRow("照片", binding(\.includePhotos))
                divider
                toggleRow("视频", binding(\.includeVideos))
                divider
                toggleRow("实况照片", binding(\.includeLivePhotos))
                divider
                toggleRow("包含隐藏照片", binding(\.includeHidden))
                divider
                toggleRow("允许从 iCloud 下载原件", binding(\.allowICloudDownload))
                divider
                toggleRow("跳过服务器已有的照片", binding(\.skipExistingOnServer))
                divider

                Stepper(value: binding(\.concurrentUploads), in: 1...4) {
                    HStack {
                        Text("并发上传")
                            .font(.footnote)
                            .foregroundStyle(.primary)
                        Spacer()
                        Text("\(settings.options.concurrentUploads) 个")
                            .font(.system(.footnote, design: .rounded, weight: .semibold))
                            .monospacedDigit()
                            .foregroundStyle(Theme.accent)
                    }
                }
                .padding(.vertical, 7)
                .tint(Theme.accent)

                divider

                Toggle("只同步指定日期之后", isOn: fromDateEnabled)
                    .font(.footnote)
                    .tint(Theme.accent)
                    .padding(.vertical, 3)

                if settings.options.fromDate != nil {
                    DatePicker("起始日期", selection: fromDateBinding, displayedComponents: .date)
                        .font(.footnote)
                        .datePickerStyle(.compact)
                        .tint(Theme.accent)
                }

                Text("上传的是原始文件；服务器按内容去重，重复的会记为「跳过」，不会改动本机相册。")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .padding(.top, 8)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var divider: some View {
        VaultDivider(vertical: false).padding(.vertical, 1)
    }

    private func toggleRow(_ title: String, _ isOn: Binding<Bool>) -> some View {
        Toggle(title, isOn: isOn)
            .font(.footnote)
            .tint(Theme.accent)
            .padding(.vertical, 6)
    }

    private func binding<Value>(_ keyPath: WritableKeyPath<SyncOptions, Value>) -> Binding<Value> {
        Binding(
            get: { settings.options[keyPath: keyPath] },
            set: { settings.options[keyPath: keyPath] = $0 }
        )
    }

    private var fromDateEnabled: Binding<Bool> {
        Binding(
            get: { settings.options.fromDate != nil },
            set: { enabled in
                if enabled {
                    settings.options.fromDate = Calendar.current.date(byAdding: .year, value: -1, to: Date())
                } else {
                    settings.options.fromDate = nil
                }
            }
        )
    }

    private var fromDateBinding: Binding<Date> {
        Binding(
            get: { settings.options.fromDate ?? Date() },
            set: { settings.options.fromDate = $0 }
        )
    }
}

// MARK: - 相册权限

private struct PermissionSettingsCard: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openURL) private var openURL

    private var engine: SyncEngine { model.engine }
    private var status: PhotoAuthorizationStatus { engine.authorizationStatus }

    var body: some View {
        VaultCard {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 7) {
                    Image(systemName: "photo.on.rectangle.angled")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(Theme.accent)
                    Text("相册权限")
                        .font(.system(.subheadline, design: .rounded, weight: .semibold))
                    Spacer()
                    HStack(spacing: 5) {
                        StatusDot(color: statusColor)
                        Text(status.title)
                            .font(.system(.caption2, design: .rounded, weight: .semibold))
                            .foregroundStyle(statusColor)
                    }
                }

                Text(statusDescription)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                HStack(spacing: 10) {
                    if status == .notDetermined || status == .unknown {
                        Button {
                            Task { await engine.requestAuthorization() }
                        } label: {
                            Label("允许访问相册", systemImage: "checkmark.shield")
                        }
                        .buttonStyle(VaultActionButtonStyle(variant: .primary))
                        .accessibilityLabel("请求相册访问权限")
                    }

                    if status == .limited {
                        Button {
                            Task { await engine.presentLimitedLibraryPicker() }
                        } label: {
                            Label("管理可访问照片", systemImage: "photo.badge.checkmark")
                        }
                        .buttonStyle(VaultActionButtonStyle(variant: .primary))
                        .accessibilityLabel("管理可访问的照片")
                    }

                    if status != .authorized {
                        Button {
                            openSystemSettings()
                        } label: {
                            Label("系统设置", systemImage: "gearshape")
                        }
                        .buttonStyle(VaultActionButtonStyle(variant: .secondary))
                        .accessibilityLabel("打开系统设置")
                    } else {
                        Button {
                            Task { await engine.refreshLocalStats() }
                        } label: {
                            Label("刷新本机统计", systemImage: "arrow.clockwise")
                        }
                        .buttonStyle(VaultActionButtonStyle(variant: .secondary))
                        .accessibilityLabel("刷新本机相册统计")
                    }
                }
            }
        }
    }

    private var statusColor: Color {
        switch status {
        case .authorized: return Theme.success
        case .limited: return Theme.warning
        case .denied, .restricted: return Theme.danger
        case .notDetermined, .unknown: return Color.gray
        }
    }

    private var statusDescription: String {
        switch status {
        case .authorized:
            return "已获得完整相册访问权限，可以同步全部照片和视频。"
        case .limited:
            return "系统只允许 App 访问你选中的部分照片。可以随时重新选择，或改成完整访问。"
        case .notDetermined:
            return "还没有授权。允许后 App 才能读取相册并上传。"
        case .denied:
            return "你拒绝了相册访问。需要到系统设置里打开才能同步。"
        case .restricted:
            return "系统限制了这个 App 访问相册（家长控制或 MDM）。"
        case .unknown:
            return "正在确认权限状态；如果一直停着，点下面按钮重新请求一次。"
        }
    }

    private func openSystemSettings() {
        guard let url = URL(string: UIApplication.openSettingsURLString) else { return }
        openURL(url)
    }
}

// MARK: - 诊断日志

private struct DiagnosticsCard: View {
    @State private var copied = false

    private var entries: [AppLog.Entry] {
        Array(AppLog.shared.entries.suffix(120).reversed())
    }

    var body: some View {
        VaultCard {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 7) {
                    Image(systemName: "waveform.path.ecg")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(Theme.accent)
                    Text("诊断日志")
                        .font(.system(.subheadline, design: .rounded, weight: .semibold))
                    Spacer()
                    Button {
                        UIPasteboard.general.string = AppLog.shared.text
                        copied = true
                        Haptics.success()
                        Task {
                            try? await Task.sleep(nanoseconds: 1_500_000_000)
                            copied = false
                        }
                    } label: {
                        Label(copied ? "已复制" : "复制", systemImage: copied ? "checkmark" : "doc.on.doc")
                    }
                    .buttonStyle(VaultMiniButtonStyle(tint: copied ? Theme.success : Color.white.opacity(0.85)))
                    .accessibilityLabel("复制全部诊断日志")

                    Button {
                        AppLog.shared.clear()
                    } label: {
                        Text("清空")
                    }
                    .buttonStyle(VaultMiniButtonStyle())
                    .accessibilityLabel("清空诊断日志")
                }

                if AppLog.shared.entries.isEmpty {
                    Text("暂无日志。连接测试、同步过程都会记录在这里。")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .padding(.vertical, 8)
                } else {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 4) {
                            ForEach(entries) { entry in
                                HStack(alignment: .top, spacing: 6) {
                                    Text(TimeText.clock(entry.date))
                                        .foregroundStyle(.tertiary)
                                    Text(entry.level.symbol)
                                        .foregroundStyle(color(for: entry.level))
                                    Text(entry.message)
                                        .foregroundStyle(Color.white.opacity(0.62))
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                .font(.system(size: 10, design: .monospaced))
                                .frame(maxWidth: .infinity, alignment: .leading)
                            }
                        }
                        .padding(10)
                    }
                    .frame(maxHeight: 200)
                    .background(
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .fill(Color.black.opacity(0.28))
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .strokeBorder(Color.white.opacity(0.06), lineWidth: 1)
                    )
                }
            }
        }
    }

    private func color(for level: AppLog.Level) -> Color {
        switch level {
        case .info: return .secondary
        case .success: return Theme.success
        case .warn: return Theme.warning
        case .error: return Theme.danger
        }
    }
}

// MARK: - 关于

private struct AboutCard: View {
    var body: some View {
        VaultCard {
            VStack(alignment: .leading, spacing: 11) {
                HStack(spacing: 7) {
                    Image(systemName: "info.circle")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(Theme.accent)
                    Text("关于")
                        .font(.system(.subheadline, design: .rounded, weight: .semibold))
                }

                InfoRow(label: "版本", value: versionText)
                VaultDivider(vertical: false)
                InfoRow(label: "默认地址", value: ServerEndpoint.defaultAddress)
                VaultDivider(vertical: false)
                InfoRow(label: "同步行为", value: "上传原始文件 · 服务端去重 · 不改本机相册")

                Text("这是一个把 iPhone 相册备份到自建 Photo Vault 的客户端。服务器地址填家里电脑的局域网地址即可；如果是单段主机名，App 会自动尝试补 .local。")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 2)
            }
        }
    }

    private var versionText: String {
        let short = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "1.0"
        let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "1"
        return "\(short) (\(build))"
    }
}
