package platform

import "os"

// required returns an environment variable that must be set, panicking at startup when it is missing.
func Required(key string) string {
	value := os.Getenv(key)
	if value == "" {
		panic("missing configuration: " + key)
	}
	return value
}

// env returns an environment variable, or fallback when it is unset.
func Env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
