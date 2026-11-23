import { useRouter } from 'vue-router'
import { api, errorText } from '@/api'
import type { Asset, Library } from '@/types'
import { fmtBytes } from '@/utils/format'
import { useBinStore } from '@/stores/bin'
import { useDevicesStore } from '@/stores/devices'
import { useDialogStore } from '@/stores/dialog'
import { useFeedStore } from '@/stores/feed'
import { useFoldersStore } from '@/stores/folders'
import { useRevealStore } from '@/stores/reveal'
import { useSelectionStore } from '@/stores/selection'
import { useSystemStore } from '@/stores/system'
import { useToastStore } from '@/stores/toast'

/**
 * 资产的所有写操作都从这里走。
 *
 * 组件只负责「点了哪个按钮」，这里负责：调接口 → 同步本地状态 → 提示/撤销。
 * 这样格子、灯箱、多选工具条上的同一个动作不会各写一遍。
 */
export function useAssetActions() {
  const router = useRouter()
  const feed = useFeedStore()
  const selection = useSelectionStore()
  const bin = useBinStore()
  const toast = useToastStore()
  const system = useSystemStore()
  const dialog = useDialogStore()

  async function setFavorite(ids: string[], value: boolean) {
    if (!ids.length) return
    const res = await api.batch({ ids, action: value ? 'favorite' : 'unfavorite' })
    ids.forEach(id => feed.toggleFavorite(id, value))
    toast.show(value ? `已喜欢 ${res.updated} 项` : `已取消喜欢 ${res.updated} 项`)
  }

  /** 单个资产切换喜欢；灯箱和格子都调它。 */
  async function toggleFavorite(asset: Asset) {
    const res = await api.favorite(asset.id, !asset.favorite)
    asset.favorite = res.favorite
    feed.toggleFavorite(asset.id, res.favorite)
  }

  async function trash(ids: string[]) {
    if (!ids.length) return
    const res = await api.batch({ ids, action: 'trash' })
    feed.removeMany(ids)
    selection.stop()
    toast.show(`已将 ${res.updated} 项移入最近删除`, '撤销', async () => {
      await api.batch({ ids, action: 'restore' })
      feed.requestReload()
      await system.refreshStats()
    })
    await system.refreshStats()
  }

  async function restore(ids: string[]) {
    if (!ids.length) return
    const res = await api.batch({ ids, action: 'restore' })
    feed.removeMany(ids)
    selection.stop()
    toast.show(`已恢复 ${res.updated} 项`)
    await system.refreshStats()
  }

  /**
   * 隐藏 / 取消隐藏。默认视图（「展示隐藏」开关关闭）里隐藏的项直接从列表消失；
   * 开关打开时只是角标变化，项留在列表里。撤销 = 批量取消隐藏并重拉当前列表。
   */
  async function setHidden(ids: string[], value: boolean) {
    if (!ids.length) return
    const reveal = useRevealStore()
    const res = await api.batch({ ids, action: value ? 'hide' : 'unhide' })
    ids.forEach(id => feed.patch(id, { hidden: value }))
    if (value && !reveal.showHidden) feed.removeMany(ids)
    selection.stop()
    toast.show(
      value ? `已隐藏 ${res.updated} 项` : `已取消隐藏 ${res.updated} 项`,
      value ? '撤销' : '',
      value
        ? async () => {
            await api.batch({ ids, action: 'unhide' })
            feed.requestReload()
          }
        : undefined,
    )
  }

  async function purge(ids: string[]) {
    if (!ids.length) return
    const ok = await dialog.confirm({
      title: `彻底删除 ${ids.length} 项？`,
      message: '库记录会被移除，不可恢复。磁盘上的原件不会被动。',
      confirmText: '彻底删除',
      danger: true,
    })
    if (!ok) return
    await Promise.all(ids.map(id => api.purge(id)))
    feed.removeMany(ids)
    selection.stop()
    toast.show(`已彻底删除 ${ids.length} 项`)
    await system.refreshStats()
  }

  /** 换归属，不是复制：AI 结果（标签/人脸/视频摘要）跟着资产走。 */
  async function moveTo(library: Library) {
    const ids = selection.list()
    if (!ids.length) return
    try {
      const res = await api.batch({ ids, action: 'move', library })
      feed.removeMany(ids)
      selection.stop()
      await system.refreshStats()
      toast.show(
        library === 'material' ? `已移入素材库 ${res.updated} 项` : `已移回图库 ${res.updated} 项`,
        '去看看',
        () => router.push(library === 'material' ? '/material' : '/gallery'),
      )
    } catch (e) {
      toast.show('移动失败：' + errorText(e).slice(0, 60))
    }
  }

  /**
   * 移入 / 移出素材库文件夹。folderId=null = 移出，素材回到「未整理」。
   * 在「未整理 / 某个文件夹」这类按文件夹切的流里，资产会离开当前视图，本地直接删行；
   * 按时间（整个素材库）这类全库视图里资产还在库里，只是换了归属，列表不用动。
   */
  async function setFolder(ids: string[], folderId: string | null) {
    if (!ids.length) return
    const folders = useFoldersStore()
    try {
      const res = await api.batch({
        ids,
        action: folderId ? 'set_folder' : 'clear_folder',
        folder_id: folderId,
      })
      if (['unfiled', 'folder'].includes(feed.config.kind)) feed.removeMany(ids)
      selection.stop()
      // 文件夹的 count/cover 变了；未整理计数也一样（按当前库重拉）
      await folders.load(feed.config.library ?? 'gallery', true)
      toast.show(
        folderId
          ? `已移入「${folders.nameOf(folderId) || '文件夹'}」${res.updated} 项`
          : `已移出文件夹 ${res.updated} 项`,
      )
    } catch (e) {
      toast.show('移动失败：' + errorText(e).slice(0, 60))
    }
  }

  async function enqueueAI(ids: string[]) {
    if (!ids.length) return toast.show('没有可识别的素材')
    try {
      const res = await api.analyze(ids)
      if (res.queued) toast.show(`已排队 ${res.queued} 个 AI 任务（打标 / 人脸 / 视频）`)
      else toast.show('这些素材已经识别过，或者已经在队列里了')
    } catch (e) {
      toast.show('入队失败：' + errorText(e).slice(0, 60))
    }
  }

  async function addToBin(ids: string[]) {
    if (!ids.length) return
    try {
      const res = await bin.add(ids)
      selection.stop()
      const extra = res.moved ? `，${res.moved} 项移到末尾` : ''
      toast.show(`已加入剪辑箱 ${res.added} 项${extra}`)
    } catch (e) {
      toast.show('加入剪辑箱失败：' + errorText(e).slice(0, 60))
    }
    await bin.refreshSummary()
  }

  async function removeFromBin(assetId: string) {
    await bin.remove(assetId)
    toast.show('已移出剪辑箱', '撤销', async () => {
      await bin.add([assetId])
      await bin.loadItems()
    })
  }

  async function clearBin() {
    const ok = await dialog.confirm({
      title: '清空剪辑箱？',
      message: '素材库和已经出稿的目录都不受影响。',
      confirmText: '清空',
      danger: true,
    })
    if (!ok) return
    await bin.clear()
    toast.show('剪辑箱已清空')
  }

  async function deliverBin() {
    if (!bin.count) return toast.show('剪辑箱是空的')
    try {
      const result = await bin.deliver()
      let opened = result.opened
      if (!opened) opened = await system.revealOnHost(result.host_dir)
      if (!opened) {
        try {
          await navigator.clipboard.writeText(result.host_dir)
        } catch {
          /* 剪贴板不可用 */
        }
      }
      let msg = `已出稿 ${result.items} 项 · ${fmtBytes(result.bytes)}`
      if (result.missing) msg += ` · ${result.missing} 项原件缺失已跳过`
      msg += opened ? ' · 已打开文件夹' : ' · 桥未启动，路径已复制'
      toast.show(msg, '复制路径', () => navigator.clipboard.writeText(result.host_dir))
      if (!opened) await system.checkBridge()
      return result
    } catch (e) {
      toast.show('出稿失败：' + errorText(e).slice(0, 80))
      return null
    }
  }

  async function batchTag(ids: string[], name: string, add: boolean) {
    if (!ids.length) return
    const res = await api.batch({ ids, action: add ? 'tag' : 'untag', tag: name })
    toast.show(add ? `已为 ${res.updated} 项添加「${name}」` : `已移除 ${res.updated} 项的「${name}」`)
  }

  /** 批量设置/清除拍摄设备；deviceId=null 表示回到「未识别」。 */
  async function setDevice(ids: string[], deviceId: number | null) {    if (!ids.length) return 0
    const devices = useDevicesStore()
    const res = await api.assignDevice(ids, deviceId)
    if (deviceId != null) await devices.load(true)
    const device = deviceId == null ? null : devices.byId.get(deviceId) ?? null
    ids.forEach(id => {
      const asset = feed.items.find(a => a.id === id)
      if (!asset) return
      asset.device_id = deviceId
      asset.device_unknown = deviceId == null
      asset.model = device?.name ?? '未识别设备'
    })
    selection.stop()
    toast.show(
      deviceId == null
        ? `已清除 ${res.updated} 项的设备`
        : `已把 ${res.updated} 项设为「${device?.name ?? '设备'}」`,
    )
    return res.updated
  }

  return {
    setFavorite,
    toggleFavorite,
    trash,
    restore,
    setHidden,
    purge,
    moveTo,
    setFolder,
    enqueueAI,
    addToBin,
    removeFromBin,
    clearBin,
    deliverBin,
    batchTag,
    setDevice,
  }
}
