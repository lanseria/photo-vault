//
//  AssetDetailView.swift
//  PhotoVaultSync
//
//  服务器资产详情（只读）：大图（/preview/{id}，视频回退缩略图）+ 拍摄时间 / 设备 /
//  尺寸 / 时长 / 体积 / 类型 / 标签。
//

import SwiftUI

struct AssetDetailView: View {
    let asset: RemoteAsset
    let thumbURL: URL?
    let previewURL: URL?

    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ZStack {
            VaultBackground()

            VStack(spacing: 0) {
                topBar

                ScrollView {
                    VStack(spacing: 16) {
                        imageArea
                        metaCard
                        if asset.tags.isEmpty {
                            EmptyView()
                        } else {
                            tagsCard
                        }
                        Text("只读浏览 · 不会修改服务器上的任何内容")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                            .padding(.top, 2)
                    }
                    .padding(.horizontal, Theme.hPadding)
                    .padding(.bottom, 30)
                }
                .scrollIndicators(.hidden)
            }
        }
        .presentationDragIndicator(.visible)
        .presentationBackground(Color(red: 0.055, green: 0.070, blue: 0.098))
    }

    // MARK: - 顶栏

    private var topBar: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 5) {
                    Image(systemName: AssetKind.icon(asset.kind))
                        .font(.system(size: 10, weight: .semibold))
                    Text(AssetKind.label(asset.kind))
                        .font(.system(size: 10, weight: .semibold, design: .rounded))
                }
                .foregroundStyle(Theme.accent)

                Text(asset.fileName)
                    .font(.system(.subheadline, design: .monospaced, weight: .medium))
                    .lineLimit(1)
                    .truncationMode(.middle)
            }

            Spacer(minLength: 8)

            Button {
                dismiss()
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Color.white.opacity(0.75))
                    .frame(width: 32, height: 32)
                    .background(Circle().fill(Color.white.opacity(0.08)))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("关闭详情")
        }
        .padding(.horizontal, Theme.hPadding)
        .padding(.top, 18)
        .padding(.bottom, 12)
    }

    // MARK: - 大图

    private var imageArea: some View {
        VStack(spacing: 8) {
            RemoteImage(
                thumbURL: thumbURL,
                previewURL: previewURL,
                prefersPreview: true,
                contentMode: .fit,
                showsProgress: true
            )
            .frame(height: 420)
            .frame(maxWidth: .infinity)
            .background(Color.black.opacity(0.4))
            .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .strokeBorder(Color.white.opacity(0.08), lineWidth: 1)
            )

            if asset.isVideo {
                Text("视频预览服务端暂不支持，这里显示的是缩略图")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
    }

    // MARK: - 元数据

    private var metaCard: some View {
        VaultCard {
            VStack(spacing: 11) {
                InfoRow(label: "拍摄时间", value: ServerDate.displayText(asset.taken))
                VaultDivider(vertical: false)
                InfoRow(label: "设备", value: asset.model.isEmpty ? "未识别" : asset.model)
                VaultDivider(vertical: false)
                InfoRow(label: "尺寸", value: asset.pixelSizeText.isEmpty ? "未知" : asset.pixelSizeText)
                if let duration = asset.durationText {
                    VaultDivider(vertical: false)
                    InfoRow(label: "时长", value: duration)
                }
                VaultDivider(vertical: false)
                InfoRow(label: "体积", value: Format.bytes(asset.bytes))
                VaultDivider(vertical: false)
                InfoRow(label: "类型", value: AssetKind.label(asset.kind))
                VaultDivider(vertical: false)
                InfoRow(label: "归属库", value: asset.library == "material" ? "素材库" : "相册")
                if !asset.rawExt.isEmpty {
                    VaultDivider(vertical: false)
                    InfoRow(label: "RAW", value: "\(asset.rawExt.uppercased()) · \(Format.bytes(asset.rawBytes))")
                }
            }
        }
    }

    private var tagsCard: some View {
        VaultCard {
            VStack(alignment: .leading, spacing: 10) {
                Text("标签")
                    .font(.system(.caption, design: .rounded, weight: .semibold))
                    .foregroundStyle(.secondary)
                FlowRow(spacing: 6) {
                    ForEach(asset.tags, id: \.self) { tag in
                        Text(tag)
                            .font(.system(.caption2, design: .rounded, weight: .medium))
                            .foregroundStyle(Color.white.opacity(0.8))
                            .padding(.horizontal, 9)
                            .padding(.vertical, 4)
                            .background(Capsule().fill(Color.white.opacity(0.07)))
                            .overlay(Capsule().strokeBorder(Color.white.opacity(0.08)))
                    }
                }
            }
        }
    }
}

// MARK: - 简易换行布局

/// 少量子视图的自动换行（标签用；不用 Layout 协议，简单可靠）
struct FlowRow: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? 320
        var rowWidth: CGFloat = 0
        var totalHeight: CGFloat = 0
        var rowHeight: CGFloat = 0

        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if rowWidth + size.width > width, rowWidth > 0 {
                totalHeight += rowHeight + spacing
                rowWidth = 0
                rowHeight = 0
            }
            rowWidth += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
        totalHeight += rowHeight
        return CGSize(width: width, height: totalHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize,
                       subviews: Subviews, cache: inout ()) {
        var x = bounds.minX
        var y = bounds.minY
        var rowHeight: CGFloat = 0

        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x + size.width > bounds.maxX, x > bounds.minX {
                x = bounds.minX
                y += rowHeight + spacing
                rowHeight = 0
            }
            subview.place(at: CGPoint(x: x, y: y), anchor: .topLeading,
                          proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}
