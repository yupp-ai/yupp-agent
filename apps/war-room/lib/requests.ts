import 'server-only'

export function getIpAddressFromHeader(headersList: {
  get(key: string): string | null
}) {
  return (
    headersList.get('x-vercel-forwarded-for') ||
    headersList.get('x-forwarded-for')?.split(',')[0]?.trim() ||
    headersList.get('x-real-ip') ||
    headersList.get('x-client-ip') ||
    null
  )
}
