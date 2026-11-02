/** 浏览器能接住的媒体扩展名；后端还会再校验一次（meta.is_media）。 */
export const UPLOAD_EXT =
  /\.(jpe?g|jfif|jpe|png|heic|heif|webp|tiff?|bmp|gif|avif|dng|cr2|cr3|nef|arw|raf|orf|rw2|pef|srw|mp4|mov|m4v|avi|mkv|insv|360|lrv|3gp|mpeg|mpg|flv|wmv|mts|m2ts)$/i

export interface DroppedFile {
  file: File
  /** 拖文件夹进来时保留相对路径，入库后 origin 更好认 */
  name: string
}

function readEntryFile(entry: FileSystemFileEntry): Promise<File | null> {
  return new Promise(resolve => entry.file(file => resolve(file), () => resolve(null)))
}

function readDirectory(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  const out: FileSystemEntry[] = []
  return new Promise(resolve => {
    const step = () => {
      reader.readEntries(
        entries => {
          if (!entries.length) return resolve(out)
          out.push(...entries)
          step()
        },
        () => resolve(out),
      )
    }
    step()
  })
}

async function walk(entry: FileSystemEntry, prefix: string, out: DroppedFile[]): Promise<void> {
  if (entry.isFile) {
    const file = await readEntryFile(entry as FileSystemFileEntry)
    if (file) out.push({ file, name: prefix + file.name })
    return
  }
  if (!entry.isDirectory) return
  const dir = entry as FileSystemDirectoryEntry
  const children = await readDirectory(dir.createReader())
  for (const child of children) await walk(child, prefix + dir.name + '/', out)
}

/**
 * 拖拽落点里的文件列表。webkitGetAsEntry 必须在 drop 事件里同步取，
 * 一旦 await 过 DataTransferItem 就失效了 —— 所以先同步收集 entry，再展开。
 * 只在导入对话框开着的时候用（App.vue 的护栏只拦默认行为，不接文件）。
 */
export async function filesFromDrop(dt: DataTransfer | null): Promise<DroppedFile[]> {
  if (!dt) return []
  const entries: FileSystemEntry[] = []
  for (const item of Array.from(dt.items || [])) {
    if (item.kind !== 'file') continue
    const entry = item.webkitGetAsEntry?.()
    if (entry) entries.push(entry)
  }
  if (!entries.length) {
    return Array.from(dt.files || []).map(file => ({ file, name: file.name }))
  }
  const out: DroppedFile[] = []
  for (const entry of entries) await walk(entry, '', out)
  return out
}

export function newBatchId(): string {
  try {
    return crypto.randomUUID().replace(/-/g, '')
  } catch {
    return Date.now().toString(36) + Math.random().toString(36).slice(2)
  }
}
