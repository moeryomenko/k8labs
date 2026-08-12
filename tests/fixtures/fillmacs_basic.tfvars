control_plane = {
  name = "cp1"
  cpu  = 2
  ram  = 2048
  disk = 20480
  mac  = "c6:e5:50:1c:ec:01"
}

workers = [
  {
    name = "w1"
    cpu  = 2
    ram  = 4096
    disk = 40960
    mac  = "c6:e5:50:1c:ec:02"
  },
  {
    name = "w2"
    cpu  = 4
    ram  = 4096
    disk = 40960
    mac  = "c6:e5:50:1c:ec:03"
  },
  {
    name = "w3"
    cpu  = 4
    ram  = 8192
    disk = 81920
  }
]
