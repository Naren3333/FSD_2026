// Command grading is the Grading service: draft scores computed from submissions, with AI-suggested marks for the
// questions that need the teacher when an assessment asks for them, which a classroom's teacher marks, finalizes
// and releases to students.
package main

import (
	"context"
	"embed"
	"errors"
	"learning/platform"
	"sync/atomic"
)

//go:embed migrations/*.sql
var migrations embed.FS

func main() {
	platform.Serve("grading", migrations, platform.EventMigration, func(a *platform.App) ([]func(context.Context), error) {
		identity := a.Identity()
		server{store: pgStore{a.DB}, access: identity, roster: identity}.routes(a.Mux)
		// Ready only while all six consumers run: a stalled one would otherwise go unnoticed.
		var submissionsRunning, classroomsRunning, assessmentsRunning, enrollmentsRunning, dueRunning, usersRunning atomic.Bool
		a.Checks = append(a.Checks, func(ctx context.Context) error {
			if !submissionsRunning.Load() || !classroomsRunning.Load() || !assessmentsRunning.Load() || !enrollmentsRunning.Load() || !dueRunning.Load() || !usersRunning.Load() {
				return errors.New("a consumer is stopped")
			}
			return platform.KafkaReady(ctx)
		})
		submissions := platform.Consumer{DB: a.DB, Group: "grading-submitted-v4"}
		classrooms := platform.Consumer{DB: a.DB, Group: "grading-classrooms-v1"}
		assessments := platform.Consumer{DB: a.DB, Group: "grading-assessments-v1"}
		enrollments := platform.Consumer{DB: a.DB, Group: "grading-enrollments-v1"}
		dueDates := platform.Consumer{DB: a.DB, Group: "grading-due-dates-v1"}
		users := platform.Consumer{DB: a.DB, Group: "grading-users-v1"}
		marking := marker{a.DB, aiSuggester{platform.NewAICore()}}
		return []func(context.Context){
			platform.Outbox{DB: a.DB}.Run,
			func(ctx context.Context) { submissions.Consume(ctx, topicSubmitted, consume, &submissionsRunning) },
			marking.Run,
			marking.Run,
			func(ctx context.Context) {
				classrooms.Consume(ctx, platform.TopicClassroomDeleted, purge, &classroomsRunning)
			},
			func(ctx context.Context) {
				assessments.Consume(ctx, topicAssessmentDeleted, purgeAssessment, &assessmentsRunning)
			},
			func(ctx context.Context) {
				enrollments.Consume(ctx, platform.TopicEnrollmentRemoved, removeStudent, &enrollmentsRunning)
			},
			func(ctx context.Context) { dueDates.Consume(ctx, topicDueChanged, changeDue, &dueRunning) },
			func(ctx context.Context) { users.Consume(ctx, platform.TopicUserDeleted, purgeUser, &usersRunning) },
		}, nil
	})
}
