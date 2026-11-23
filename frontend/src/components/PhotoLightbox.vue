<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api, errorText } from '@/api'
import type { Asset, AssetInfo, AssetPerson, Face, Person, VideoInfo } from '@/types'
import { takenDateText } from '@/utils/date'
import { fmtBytes, fmtDur, fmtFrameTime, fmtShutter, fmtStamp, initials, sleep } from '@/utils/format'
import { useAssetActions } from '@/composables/useAssetActions'
import { lockScroll, unlockScroll } from '@/composables/scrollMemory'
import { useDevicesStore } from '@/stores/devices'
import { usePeopleStore } from '@/stores/people'
import { useSystemStore } from '@/stores/system'
import { useTagSheetStore } from '@/stores/tagSheet'
import { useToastStore } from '@/stores/toast'
import AppIcon from './AppIcon.vue'
import DevicePicker from './DevicePicker.vue'
import PersonPicker from './PersonPicker.vue'

const props = defineProps<{
  asset: Asset
  /** 当前列表，用来算上一张/下一张 */
  items: Asset[]
  /** 回收站里只显示「恢复 / 彻底删除」 */
  trashView?: boolean
}>()

const emit = defineEmits<{
  close: []
  prev: []
  next: []
  trash: []
  restore: []
  purge: []
  hidden: []
}>()

const router = useRouter()
const actions = useAssetActions()
const sheet = useTagSheetStore()
const devices = useDevicesStore()
const peopleStore = usePeopleStore()
const system = useSystemStore()
const toast = useToastStore()

const faces = ref<Face[]>([])
const video = ref<VideoInfo | null>(null)
const livePlaying = ref(false)
const videoEl = ref<HTMLVideoElement | null>(null)
const pickingDevice = ref(false)

/* ---------------- 修人脸归属：✎ 打开人物选择器，已归类的走 move，未归类走 assign ---------------- */
const editFace = ref<Face | null>(null)
const pickerPersons = ref<Person[]>([])
const movingFace = ref(false)

async function startFaceEdit(face: Face) {
  editFace.value = face
  // 排除当前归属（移动到自己没有意义）；未归类则全部人物都可选
  await peopleStore.load()
  pickerPersons.value = peopleStore.persons.filter(p => p.id !== face.person_id)
}

/* ---------------- 关联人物：没检出人脸（背影/远景）但确实与人物有关的照片视频，
   手动挂到人物名下；人物页照片流 / 计数把关联素材一起算 ---------------- */

/** 这张媒体相关的人物（含 source=face 的：脸 chips 已展示，这里只渲染手动关联的） */
const linked = ref<AssetPerson[]>([])
const linkingPerson = ref(false)

const linkedPersons = computed(() => linked.value.filter(p => p.source === 'link'))

/** 关联候选：排除已相关的（再挂没有意义） */
const linkCandidates = computed(() => {
  const taken = new Set(linked.value.map(p => p.id))
  return peopleStore.persons.filter(p => !taken.has(p.id))
})

function loadPersons(id: string) {
  api
    .assetPersons(id)
    .then(res => {
      if (props.asset.id === id) linked.value = res.persons
    })
    .catch(() => undefined)
}

/** 关联 chips 的头像：人物列表里有就用封面（自定义封面 / 人脸裁剪），没有退字母 */
function personAvatar(id: string): string {
  const hit = peopleStore.byId.get(id)
  return hit ? peopleStore.coverSrc(hit) : ''
}

async function openLinkPicker() {
  if (props.trashView || linkingPerson.value) return
  await peopleStore.load()
  linkingPerson.value = true
}

async function onLinkTarget(person: Person) {
  linkingPerson.value = false
  try {
    const res = await api.linkPerson(props.asset.id, person.id)
    linked.value = res.persons
    toast.show(`已关联到「${person.name || '未命名'}」，人物页会看到这张`)
    // 人物的计数变了
    void peopleStore.load(true)
  } catch (e) {
    toast.show('关联失败：' + errorText(e).slice(0, 60))
  }
}

async function unlinkPerson(p: AssetPerson) {
  try {
    const res = await api.unlinkPerson(props.asset.id, p.id)
    linked.value = res.persons
    toast.show('已取消关联')
    void peopleStore.load(true)
  } catch (e) {
    toast.show('取消关联失败：' + errorText(e).slice(0, 60))
    loadPersons(props.asset.id)
  }
}

/* ---------------- 手动 AI 人脸识别：对没识别过的照片单独入队，轮询等 worker 写回 ---------------- */

const detecting = ref(false)

/** 没有脸的照片才需要手动识别（有脸的用 ✎ 修归属；视频不在网页跑人脸） */
const canDetect = computed(
  () =>
    !props.trashView &&
    faces.value.length === 0 &&
    (props.asset.kind === 'photo' || props.asset.kind === 'live_photo'),
)

async function detectFaces() {
  if (detecting.value) return
  const id = props.asset.id
  detecting.value = true
  try {
    await api.runFaces(id)
    // worker 异步提取：轮询等脸写回；任务到 ok/failed 就到头了，不用干等
    const deadline = Date.now() + 90_000
    while (Date.now() < deadline) {
      await sleep(2500)
      if (props.asset.id !== id) return // 已经切到别的照片
      const res = await api.faces(id)
      if (props.asset.id !== id) return
      const found = res.faces.filter(f => !f.ignored)
      if (found.length) {
        faces.value = found
        // worker 写完新脸会紧接着跑一轮聚类，把库里已经认识的脸认领给人物；在那之前
        // person_id 还是空的。等聚类结束再收尾，不然界面停在「未归类」，照着去改会撞「已归类」
        if (res.clustering) continue
        const named = found.filter(f => f.person_id).length
        toast.show(
          named === found.length
            ? `识别到 ${found.length} 张人脸，已归到对应人物，点 ✎ 可修改`
            : `识别到 ${found.length} 张人脸${
                named ? `，其中 ${named} 张已自动归类` : ''
              }，点 ✎ 可修改归属`,
        )
        return
      }
      if (res.job === 'ok') {
        toast.show('识别完成：这张照片里没有检测到人脸')
        return
      }
      if (res.job === 'failed' || res.job === 'dead') {
        toast.show('识别失败：这张照片处理不了')
        return
      }
    }
    toast.show('识别已加入队列，还在后台进行，稍后重新打开即可看到')
  } catch (e) {
    toast.show('入队失败：' + errorText(e).slice(0, 60))
  } finally {
    detecting.value = false
  }
}

const index = computed(() => props.items.findIndex(a => a.id === props.asset.id))
const hasPrev = computed(() => index.value > 0)
const hasNext = computed(() => index.value >= 0 && index.value < props.items.length - 1)
const isVideo = computed(() => props.asset.kind === 'video')
const canPlayLive = computed(() => props.asset.kind === 'live_photo')
const dateText = computed(() => takenDateText(props.asset))
/** 素材库不把「未识别设备」当成需要处理的问题，只留中性的设备入口 */
const unknownDevice = computed(() => props.asset.device_unknown === true && props.asset.library !== 'material')

/* ---------------- 详细信息面板 ----------------
   常驻悬挂（默认打开，偏好记在浏览器）；窄屏（<640px）默认收起避免挡图，
   仍可用顶部 ⓘ 随时展开。 */
const INFO_KEY = 'pv.lightbox.info'
const infoShown = ref(
  typeof window !== 'undefined' && localStorage.getItem(INFO_KEY) !== '0' && window.innerWidth >= 640,
)
watch(infoShown, v => localStorage.setItem(INFO_KEY, v ? '1' : '0'))
const info = ref<AssetInfo | null>(null)

const KIND_TEXT: Record<string, string> = {
  photo: '照片',
  video: '视频',
  live_photo: '实况照片',
  raw: 'RAW 原片',
}

const TZ_SOURCE_TEXT: Record<string, string> = {
  exif: 'EXIF',
  gps: 'GPS',
  default: '兜底时区',
  user: '手动',
}

/** 时区来源 + 偏移，如「EXIF · UTC+8」；都没给就是空 */
const tzText = computed(() => {
  const parts: string[] = []
  const src = info.value?.tz_source ?? props.asset.tz_source
  if (src && TZ_SOURCE_TEXT[src]) parts.push(TZ_SOURCE_TEXT[src])
  const off = info.value?.tz_offset_min
  if (off != null) {
    const sign = off >= 0 ? '+' : '-'
    const h = Math.floor(Math.abs(off) / 60)
    const m = Math.abs(off) % 60
    parts.push(`UTC${sign}${h}${m ? ':' + String(m).padStart(2, '0') : ''}`)
  }
  return parts.join(' · ')
})

interface InfoRow {
  label: string
  value: string
  mono?: boolean
  copy?: string
}

/** 详情面板的设备名：info 拉到后给规范型号组，没拉到退回列表的展示名 */
const deviceName = computed(() => {
  const names = info.value?.device
    ? [...new Set([info.value.device.model_canonical, info.value.device.model_raw].filter(Boolean))]
    : []
  return names.join(' · ') || props.asset.model || '未识别设备'
})

/** 详细信息行：有数据才出这一行，尽量给全（拍摄/同步/来源/文件/EXIF/GPS） */
const infoRows = computed<InfoRow[]>(() => {
  const a = props.asset
  const inf = info.value
  const rows: InfoRow[] = []
  rows.push({ label: '类型', value: KIND_TEXT[a.kind] ?? a.kind })
  rows.push({
    label: '拍摄时间',
    value: takenDateText(a) + (tzText.value ? `（${tzText.value}）` : ''),
  })
  if (inf?.synced_at) rows.push({ label: '同步时间', value: fmtStamp(inf.synced_at) })
  if (inf?.added_at && inf.added_at !== inf.synced_at) {
    rows.push({ label: '入库时间', value: fmtStamp(inf.added_at) })
  }
  const src = inf?.source
  if (src?.label || src?.kind) {
    rows.push({ label: '同步来源', value: [src.label, src.kind].filter(Boolean).join(' · ') })
  }
  if (inf?.src_path) rows.push({ label: '原始地址', value: inf.src_path, mono: true, copy: inf.src_path })
  if (inf?.origin_rel) rows.push({ label: '库内位置', value: inf.origin_rel, mono: true, copy: inf.origin_rel })
  if (a.width && a.height) {
    rows.push({
      label: '分辨率',
      value: `${a.width}×${a.height}（${((a.width * a.height) / 1e6).toFixed(1)} MP）`,
    })
  }
  if (a.duration_ms) rows.push({ label: '时长', value: fmtDur(a.duration_ms) })
  const fmt = [inf?.ext, inf?.mime].filter(Boolean).join(' · ')
  if (fmt) rows.push({ label: '格式', value: fmt })
  rows.push({
    label: '大小',
    value: fmtBytes(a.bytes) + (a.raw_ext ? ` + RAW ${fmtBytes(a.raw_bytes)}` : ''),
  })
  if (a.raw_ext) {
    rows.push({
      label: 'RAW+JPEG',
      value: `${a.raw_ext} · ${fmtBytes(a.raw_bytes)}（出稿时一起给）`,
    })
  }
  if (inf?.lens) rows.push({ label: '镜头', value: inf.lens })
  const ex = inf?.exif
  if (ex) {
    const parts = [
      ex.iso != null && `ISO ${ex.iso}`,
      ex.aperture != null && `f/${ex.aperture}`,
      ex.shutter != null && fmtShutter(ex.shutter),
      ex.focal_mm != null && `${Math.round(ex.focal_mm)}mm`,
    ].filter(Boolean)
    if (parts.length) rows.push({ label: 'EXIF', value: parts.join(' · ') })
  }
  if (inf?.gps) {
    const text = `${inf.gps.lat.toFixed(6)}, ${inf.gps.lon.toFixed(6)}`
    rows.push({ label: 'GPS', value: text, mono: true, copy: text })
  }
  if (inf?.sha) rows.push({ label: '指纹 SHA-256', value: `${inf.sha.slice(0, 32)}…`, mono: true, copy: inf.sha })
  return rows
})

async function copyText(text: string, label: string) {
  try {
    await navigator.clipboard.writeText(text)
    toast.show(`${label}已复制`)
  } catch {
    toast.show('复制失败：浏览器不给剪贴板权限')
  }
}

async function loadExtras() {
  const id = props.asset.id
  faces.value = []
  linked.value = []
  video.value = null
  info.value = null
  livePlaying.value = false

  loadFaces(id)
  loadPersons(id)
  // 关联 chips 的头像要用人物封面
  void peopleStore.load()

  // 详情（拍摄/同步/来源/EXIF/GPS）预取好，点 ⓘ 面板秒开
  api
    .info(id)
    .then(res => {
      if (props.asset.id === id) info.value = res
    })
    .catch(() => undefined)

  if (props.asset.kind === 'video') {
    api
      .video(id)
      .then(res => {
        if (props.asset.id === id) video.value = res
      })
      .catch(() => undefined)
  }
}

/** 拉这张媒体的人脸（修归属后也要重拉） */
function loadFaces(id: string) {
  api
    .faces(id)
    .then(res => {
      // 已忽略的脸（误检）不再展示
      if (props.asset.id === id) faces.value = res.faces.filter(f => !f.ignored)
    })
    .catch(() => undefined)
}

/** 人物选择器选定目标：一律走 assign（未归类 / 已归到别人名下都收），完成后刷新脸和计数。
 *  手里的归属可能是旧的（worker 的聚类随时会把未归类的脸认领走），所以不按它分 assign / move */
async function onFaceTarget(person: Person) {
  const face = editFace.value
  if (!face || movingFace.value) return
  movingFace.value = true
  try {
    await api.assignFaces([face.id], person.id)
    toast.show(`这张脸已归到「${person.name || '未命名'}」`)
    editFace.value = null
    loadFaces(props.asset.id)
    // 人物的封面/计数变了，下次进人物页要用新数据
    void peopleStore.load(true)
  } catch (e) {
    toast.show('修改失败：' + errorText(e).slice(0, 60) + '，已刷新人脸，请重试')
    // 这张脸多半刚被重新识别过（id 变了）：换成最新的，重新点 ✎ 即可
    editFace.value = null
    loadFaces(props.asset.id)
  } finally {
    movingFace.value = false
  }
}

watch(() => props.asset.id, loadExtras, { immediate: true })

async function favorite() {
  await actions.toggleFavorite(props.asset)
}

function addToBin() {
  void actions.addToBin([props.asset.id])
}

function editTags() {
  sheet.show([props.asset.id])
}

async function removeTag(name: string) {
  const res = await api.removeTag(props.asset.id, name)
  props.asset.tags = res.tags
}

function seek(ms: number) {
  const el = videoEl.value
  if (!el) return
  el.currentTime = ms / 1000
  void el.play().catch(() => undefined)
}

function openPerson(personId: string) {
  emit('close')
  void router.push(`/people/${personId}`)
}

/** 手工把这张照片归到某台设备（或清除归类）。 */
async function pickDevice(deviceId: number | null) {
  pickingDevice.value = false
  if ((props.asset.device_id ?? null) === deviceId) return
  try {
    const res = await api.assignDevice([props.asset.id], deviceId)
    if (!res.updated) return
    const device = deviceId == null ? null : devices.byId.get(deviceId) ?? null
    props.asset.device_id = deviceId
    props.asset.device_unknown = deviceId == null
    props.asset.model = device?.name ?? '未识别设备'
    toast.show(device ? `设备已设为「${device.name}」` : '已标记为未识别设备')
    void devices.load(true)
    void system.refreshStats()
  } catch {
    toast.show('设置设备失败')
  }
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') emit('close')
  else if (event.key === 'ArrowLeft' && hasPrev.value) emit('prev')
  else if (event.key === 'ArrowRight' && hasNext.value) emit('next')
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown)
  lockScroll()
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown)
  unlockScroll()
})
</script>

<template>
  <div class="fixed inset-0 z-[100] flex flex-col bg-black">
    <div class="z-[3] flex items-center gap-2.5 px-3.5 py-3">
      <button class="icon-btn" aria-label="返回" @click="emit('close')">
        <AppIcon name="chevron-left" :size="26" :stroke="2.2" />
      </button>
      <div class="flex-1 text-center text-[14px] font-semibold">{{ dateText }}</div>
      <button
        class="icon-btn"
        :class="infoShown && 'bg-white/15'"
        :aria-label="infoShown ? '收起详细信息' : '详细信息'"
        :title="infoShown ? '收起详细信息' : '详细信息'"
        @click="infoShown = !infoShown"
      >
        <AppIcon name="info" :size="22" />
      </button>
    </div>

    <div class="flex min-h-0 flex-1">
      <div class="relative flex min-w-0 flex-1 items-center justify-center">
        <img
          v-if="!isVideo && !livePlaying"
          :src="`/preview/${asset.id}`"
          alt=""
          class="max-h-full max-w-full object-contain"
        />
        <video
          v-else-if="isVideo"
          ref="videoEl"
          :src="`/file/${asset.id}`"
          controls
          autoplay
          playsinline
          class="max-h-full max-w-full"
        />
        <video
          v-else
          :src="`/file/${asset.id}?role=live_motion`"
          controls
          autoplay
          playsinline
          class="max-h-full max-w-full"
        />

        <button
          v-if="hasPrev"
          class="absolute left-2.5 top-1/2 flex h-[70px] w-[46px] -translate-y-1/2 items-center justify-center rounded-xl bg-[rgba(60,60,67,.4)] text-white opacity-75 hover:opacity-100"
          aria-label="上一张"
          @click="emit('prev')"
        >
          <AppIcon name="chevron-left" :size="24" :stroke="2.4" />
        </button>
        <button
          v-if="hasNext"
          class="absolute right-2.5 top-1/2 flex h-[70px] w-[46px] -translate-y-1/2 items-center justify-center rounded-xl bg-[rgba(60,60,67,.4)] text-white opacity-75 hover:opacity-100"
          aria-label="下一张"
          @click="emit('next')"
        >
          <AppIcon name="chevron-right" :size="24" :stroke="2.4" />
        </button>
      </div>

      <!-- 详细信息：常驻悬挂在右侧（宽屏是布局的一部分，图片在剩余空间居中；
           窄屏退回覆盖式滑入）。打开灯箱时就预取好了（loadExtras） -->
      <Transition name="slide-right">
        <aside
          v-if="infoShown"
          class="flex w-[min(360px,92vw)] max-w-full shrink-0 flex-col border-l border-line bg-[rgba(16,16,18,.96)] backdrop-blur-md lt-sm:absolute lt-sm:inset-y-0 lt-sm:right-0 lt-sm:z-[8]"
        >
          <div class="flex items-center justify-between border-b border-line/60 px-4 py-3">
            <h3 class="m-0 text-[15px] font-semibold">详细信息</h3>
            <button class="icon-btn" aria-label="收起" @click="infoShown = false">
              <AppIcon name="close" :size="20" />
            </button>
          </div>
          <div class="min-h-0 flex-1 overflow-auto px-4 py-3">
            <div v-if="!info" class="spinner !my-6" />
            <dl v-else class="m-0 flex flex-col gap-3.5">
              <!-- 拍摄设备：从图片下方移进来，可直接更换归类 -->
              <div>
                <dt class="text-[11.5px] leading-tight text-mut">拍摄设备</dt>
                <dd class="m-0 mt-0.5 flex items-center gap-2">
                  <span
                    class="min-w-0 flex-1 break-all text-[13px] leading-snug"
                    :class="unknownDevice && 'text-warn'"
                    >{{ deviceName }}</span
                  >
                  <button
                    class="shrink-0 rounded-[7px] bg-panel px-2 py-0.5 text-[11px] text-acc tap"
                    @click="pickingDevice = true"
                  >
                    更换
                  </button>
                </dd>
              </div>
              <div v-for="row in infoRows" :key="row.label">
                <dt class="text-[11.5px] leading-tight text-mut">{{ row.label }}</dt>
                <dd class="m-0 mt-0.5 flex items-start gap-2">
                  <span
                    class="min-w-0 flex-1 break-all text-[13px] leading-snug"
                    :class="row.mono && 'font-mono text-[12px]'"
                    >{{ row.value }}</span
                  >
                  <button
                    v-if="row.copy"
                    class="shrink-0 pt-0.5 text-[11px] text-mut hover:text-acc"
                    @click="row.copy && copyText(row.copy, row.label)"
                  >
                    复制
                  </button>
                </dd>
              </div>
            </dl>
          </div>
        </aside>
      </Transition>
    </div>

    <div class="flex flex-col gap-2.5 px-4 pb-safe-16">
      <div
        v-if="faces.length || linkedPersons.length || !trashView"
        class="flex flex-wrap items-center gap-2"
      >
        <div
          v-for="face in faces"
          :key="face.id"
          class="inline-flex items-center gap-0.5 rounded-full bg-panel2 py-[3px] pl-[3px] pr-1 text-[13px]"
          :class="!face.person_id && 'opacity-70'"
        >
          <button
            class="inline-flex items-center gap-1.5 rounded-full tap disabled:cursor-default disabled:opacity-60"
            :disabled="!face.person_id"
            :title="face.person_id ? `查看 ${face.person || '未命名'}` : '未归类人脸'"
            @click="face.person_id && openPerson(face.person_id)"
          >
            <img
              :src="`/faces/${face.id}.jpg`"
              alt=""
              loading="lazy"
              class="h-[30px] w-[30px] rounded-full bg-panel object-cover"
            />
            <span>{{ face.person || '未归类' }}</span>
          </button>
          <button
            class="flex h-6 w-6 items-center justify-center rounded-full text-mut hover:text-acc tap"
            :title="face.person_id ? `把这张脸改归到其他人物（当前：${face.person || '未命名'}）` : '归类到人物'"
            @click="startFaceEdit(face)"
          >
            <AppIcon name="edit" :size="13" :stroke="2.2" />
          </button>
        </div>

        <!-- 手动关联的人物：没检出人脸（背影/远景…）但确实与人物有关，人工挂的 -->
        <div
          v-for="p in linkedPersons"
          :key="'link-' + p.id"
          class="inline-flex items-center gap-0.5 rounded-full bg-panel2 py-[3px] pl-[3px] pr-1 text-[13px]"
        >
          <button
            class="inline-flex items-center gap-1.5 rounded-full tap"
            :title="`查看 ${p.name || '未命名'}`"
            @click="openPerson(p.id)"
          >
            <img
              v-if="personAvatar(p.id)"
              :src="personAvatar(p.id)"
              alt=""
              loading="lazy"
              class="h-[30px] w-[30px] rounded-full bg-panel object-cover"
            />
            <span
              v-else
              class="flex h-[30px] w-[30px] items-center justify-center rounded-full bg-panel text-[11px] text-mut"
              >{{ initials(p.name || '?') }}</span
            >
            <span>{{ p.name || '未命名' }}</span>
          </button>
          <button
            class="flex h-6 w-6 items-center justify-center rounded-full text-mut hover:text-heart tap"
            title="取消关联（不影响识别出来的人脸）"
            @click="unlinkPerson(p)"
          >
            ✕
          </button>
        </div>

        <button
          v-if="!trashView"
          class="rounded-full bg-panel2 px-3 py-1 text-[13px] text-mut tap"
          title="没检出人脸但和某个人物有关（背影、远景…）？手动关联到人物"
          @click="openLinkPicker"
        >
          ＋ 关联人物
        </button>
      </div>

      <div v-if="isVideo && video?.summary" class="flex flex-col gap-1.5">
        <div v-if="video.summary.caption" class="text-[13px] leading-[1.45] opacity-90">
          AI 描述：{{ video.summary.caption }}
        </div>
        <div v-if="video.frames.length" class="no-scrollbar flex gap-1.5 overflow-x-auto pb-0.5">
          <button
            v-for="frame in video.frames"
            :key="frame.ord"
            class="relative shrink-0 rounded-lg border-2 border-transparent p-0 leading-none"
            @click="seek(frame.t_ms)"
          >
            <img
              v-if="frame.url"
              :src="frame.url"
              alt=""
              loading="lazy"
              class="block h-[52px] w-[92px] bg-panel object-cover"
            />
            <span
              class="absolute bottom-[3px] right-[3px] rounded bg-black/60 px-[3px] text-[10px] leading-[1.4] text-white"
              >{{ fmtFrameTime(frame.t_ms) }}</span
            >
          </button>
        </div>
        <div v-if="video.summary.ocr" class="max-h-[46px] overflow-auto whitespace-pre-wrap text-[12px] text-mut">
          画面文字：{{ video.summary.ocr }}
        </div>
      </div>

      <div class="flex min-h-7 flex-wrap items-center gap-1.5">
        <span
          v-for="tag in asset.tags"
          :key="tag"
          class="inline-flex items-center gap-1.5 rounded-full bg-panel2 px-3 py-1 text-[13px]"
        >
          {{ tag }}
          <button class="text-mut hover:text-heart" @click="removeTag(tag)">✕</button>
        </span>
        <button class="rounded-full bg-panel2 px-3 py-1 text-[13px] tap" @click="editTags">
          ＋ 标签
        </button>
      </div>

      <div class="flex items-center justify-around">
        <button
          v-if="!trashView"
          class="flex flex-col items-center gap-[3px] text-[11px]"
          :class="asset.favorite && 'text-heart'"
          @click="favorite"
        >
          <AppIcon name="heart" :size="24" :filled="asset.favorite" />
          {{ asset.favorite ? '取消喜欢' : '喜欢' }}
        </button>
        <button
          v-if="!trashView"
          class="flex flex-col items-center gap-[3px] text-[11px]"
          @click="addToBin"
        >
          <AppIcon name="bin" :size="24" />
          剪辑箱
        </button>
        <button
          v-if="canDetect"
          class="flex flex-col items-center gap-[3px] text-[11px] text-acc disabled:opacity-60"
          :disabled="detecting"
          title="对这张照片单独跑一次 AI 人脸识别"
          @click="detectFaces"
        >
          <AppIcon name="sparkles" :size="24" />
          {{ detecting ? '识别中…' : '识别人脸' }}
        </button>
        <button
          v-if="canPlayLive"
          class="flex flex-col items-center gap-[3px] text-[11px]"
          @click="livePlaying = !livePlaying"
        >
          <AppIcon name="play" :size="24" />
          {{ livePlaying ? '照片' : '播放实况' }}
        </button>
        <button
          v-if="!trashView"
          class="flex flex-col items-center gap-[3px] text-[11px]"
          title="隐藏后默认所有列表不显示；顶栏「展示隐藏」开关可以重新看到"
          @click="emit('hidden')"
        >
          <AppIcon :name="asset.hidden ? 'eye' : 'eyeOff'" :size="24" />
          {{ asset.hidden ? '取消隐藏' : '隐藏' }}
        </button>
        <button
          v-if="!trashView"
          class="flex flex-col items-center gap-[3px] text-[11px] text-heart"
          @click="emit('trash')"
        >
          <AppIcon name="trash" :size="24" />
          删除
        </button>
        <button
          v-if="trashView"
          class="flex flex-col items-center gap-[3px] text-[11px]"
          @click="emit('restore')"
        >
          <AppIcon name="restore" :size="24" />
          恢复
        </button>
        <button
          v-if="trashView"
          class="flex flex-col items-center gap-[3px] text-[11px] text-heart"
          @click="emit('purge')"
        >
          <AppIcon name="trash" :size="24" />
          彻底删除
        </button>
      </div>
    </div>

    <DevicePicker
      v-if="pickingDevice"
      title="这张照片的拍摄设备"
      :hint="`EXIF：${asset.device_raw || '没有机型信息'}。选一个已有设备，或者清回「未识别」。`"
      :current-id="asset.device_id ?? null"
      allow-none
      @pick="pickDevice"
      @close="pickingDevice = false"
      @manage="pickingDevice = false; emit('close'); router.push('/devices')"
    />

    <!-- 修人脸归属：✎ 选目标人物；已归类走 move，未归类走 assign -->
    <PersonPicker
      v-if="editFace"
      :people="pickerPersons"
      :title="
        editFace.person_id
          ? `把这张脸从「${editFace.person || '未命名'}」改归到…`
          : '这张脸归到哪个人物？'
      "
      @pick="onFaceTarget"
      @close="editFace = null"
    />

    <!-- 关联人物：识别不出来的（背影等）手动挂到人物名下 -->
    <PersonPicker
      v-if="linkingPerson"
      :people="linkCandidates"
      title="这张媒体和哪个人物有关？"
      @pick="onLinkTarget"
      @close="linkingPerson = false"
    />
  </div>
</template>
