type YuppEnvironment = 'development' | 'test' | 'staging' | 'production'

export const getEnvironment = (): YuppEnvironment => {
  if (
    process.env.NODE_ENV === 'development' ||
    process.env.VERCEL_ENV === 'development' ||
    process.env.NEXT_PUBLIC_VERCEL_ENV === 'development'
  ) {
    return 'development'
  }
  if (process.env.NODE_ENV === 'test' || process.env.VERCEL_ENV === 'test') {
    return 'test'
  }
  if (
    process.env.VERCEL_ENV === 'preview' ||
    process.env.NEXT_PUBLIC_VERCEL_ENV === 'preview'
  ) {
    return 'staging'
  }
  if (
    process.env.VERCEL_ENV === 'production' ||
    process.env.NEXT_PUBLIC_VERCEL_ENV === 'production'
  ) {
    return 'production'
  }
  return 'development'
}

export const isLocalDevelopment = getEnvironment() === 'development'
export const isProduction = getEnvironment() === 'production'
