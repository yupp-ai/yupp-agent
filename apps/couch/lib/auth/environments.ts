type YuppEnvironment = 'development' | 'test' | 'staging' | 'production'

export const getEnvironment = (): YuppEnvironment => {
  if (process.env.NODE_ENV === 'development') return 'development'
  if (process.env.NODE_ENV === 'test') return 'test'
  if (process.env.NODE_ENV === 'production') return 'production'
  return 'development'
}

export const isLocalDevelopment = getEnvironment() === 'development'
export const isProduction = getEnvironment() === 'production'
