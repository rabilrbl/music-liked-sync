package main

import (
	"os"

	"github.com/rabilrbl/music-liked-sync/internal/app"
)

func main() {
	if err := app.NewRootCommand().Execute(); err != nil {
		os.Exit(1)
	}
}
