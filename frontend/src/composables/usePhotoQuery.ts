import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

/** 灯箱的打开状态放在 URL（?photo=id）里：刷新能还原，浏览器上下键也自然。 */
export function usePhotoQuery() {
  const route = useRoute()
  const router = useRouter()

  const photoId = computed(() => (typeof route.query.photo === 'string' ? route.query.photo : ''))

  function update(photo: string | null, mode: 'push' | 'replace') {
    const query = { ...route.query }
    if (photo) query.photo = photo
    else delete query.photo
    if (mode === 'push') void router.push({ path: route.path, query })
    else void router.replace({ path: route.path, query })
  }

  return {
    photoId,
    open: (id: string) => update(id, 'push'),
    step: (id: string) => update(id, 'replace'),
    close: () => update(null, 'replace'),
  }
}
