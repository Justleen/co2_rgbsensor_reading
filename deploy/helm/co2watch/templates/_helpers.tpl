{{- define "co2watch.ns" -}}{{ .Values.namespace.name }}{{- end -}}

{{- define "co2watch.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{- define "co2watch.labels" -}}
app.kubernetes.io/name: co2watch
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "co2watch.vmurl" -}}
{{ required "vm.url is required, e.g. --set vm.url=http://victoriametrics.monitoring.svc.cluster.local:8428" .Values.vm.url }}
{{- end -}}

{{/* Pod-level settings shared by the toolbox and every job. */}}
{{- define "co2watch.podSpecCommon" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
securityContext:
  {{- toYaml .Values.podSecurityContext | nindent 2 }}
volumes:
  - name: data
    persistentVolumeClaim:
      claimName: co2watch-data
  - name: tmp
    emptyDir: {}
{{- end -}}

{{- define "co2watch.containerCommon" -}}
image: {{ include "co2watch.image" . }}
imagePullPolicy: {{ .Values.image.pullPolicy }}
envFrom:
  - configMapRef:
      name: co2watch-env
securityContext:
  {{- toYaml .Values.containerSecurityContext | nindent 2 }}
volumeMounts:
  - name: data
    mountPath: /data
  - name: tmp
    mountPath: /tmp
{{- end -}}
