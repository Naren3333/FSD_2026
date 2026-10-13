package main

import (
	"embed"
	"learning/platform"
	"log/slog"
	"os"
)

//go:embed migrations/*.sql
var migrations embed.FS

func main() {
	a, err := platform.New("identity", migrations, "")
	if err != nil {
		slog.Error("startup_failed", "error", err.Error())
		os.Exit(1)
	}
	s := server{a}
	a.Mux.HandleFunc("POST /v1/me", s.register)
	a.Mux.HandleFunc("GET /v1/classrooms", s.list)
	a.Mux.HandleFunc("POST /v1/classrooms", s.create)
	a.Mux.HandleFunc("GET /v1/classrooms/{id}/access", s.access)
	a.Mux.HandleFunc("POST /v1/classrooms/{id}/enrollments", s.enroll)
	if err = a.Run(); err != nil {
		slog.Error("server_failed")
		os.Exit(1)
	}
}
